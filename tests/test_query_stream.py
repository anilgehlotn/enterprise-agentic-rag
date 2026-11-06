"""POST /query/stream: Server-Sent Events from the real LangGraph streaming loop.

The graphs are real (build_graph with fake nodes), so these tests exercise LangGraph's
"updates" + "custom" stream modes and get_stream_writer(), not a hand-rolled fake.
No LLM, Portkey, Qdrant, API key or network is involved.
"""

import json
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agents import graph as graph_module
from app.agents.graph import build_graph
from app.agents.nodes import responder as responder_module
from app.agents.routing import CONVERSATIONAL
from app.agents.streaming import token_writer
from app.services import rag_service
from app.services.retrieval.schema import KNOWLEDGE_BASE_UNAVAILABLE_ANSWER, KNOWLEDGE_BASE_UNAVAILABLE_STATUS

DOC = {"source": "jobs.html", "source_type": "true", "content": "A Job runs Pods to completion.", "score": 0.8}
ANSWER_PIECES = ["Jobs ", "run Pods ", "to completion [1]."]


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """Strict SSE parser: every event is `event: <type>` + one `data: <json>` line + blank line."""
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        assert len(lines) == 2, block
        assert lines[0].startswith("event: ") and lines[1].startswith("data: "), block
        events.append((lines[0][len("event: ") :], json.loads(lines[1][len("data: ") :])))
    return events


def kinds(events):
    """Event types with consecutive repeats collapsed: step, step, token → step, token."""
    collapsed = []
    for kind, _ in events:
        if not collapsed or collapsed[-1] != kind:
            collapsed.append(kind)
    return collapsed


def text_of(events):
    return "".join(data["text"] for kind, data in events if kind == "token")


# ── Fake nodes ────────────────────────────────────────────────────────────────


class Nodes:
    def __init__(self, *, docs=True, kb_down=False, fail_in=None):
        self.docs, self.kb_down, self.fail_in = docs, kb_down, fail_in

    def _maybe_fail(self, name):
        if self.fail_in == name:
            raise RuntimeError(f"secret internal detail from {name}")

    def planner(self, state):
        self._maybe_fail("planner")
        return {"current_query": "kubernetes job", "plan": ["Intent: Technical", "Search Term: kubernetes job"]}

    def retriever(self, state):
        attempt = state.get("retrieval_attempts", 0) + 1
        if self.kb_down:
            return {
                "documents": [],
                "retrieval_attempts": attempt,
                "retrieval_error": "qdrant unreachable",
                "final_answer": KNOWLEDGE_BASE_UNAVAILABLE_ANSWER,
                "status": KNOWLEDGE_BASE_UNAVAILABLE_STATUS,
                "plan": state["plan"] + [f"Retrieval attempt {attempt}/2 (hybrid): knowledge base unavailable"],
                "messages": [{"role": "assistant", "content": KNOWLEDGE_BASE_UNAVAILABLE_ANSWER}],
            }
        docs = [DOC] if self.docs else []
        return {
            "documents": docs,
            "retrieval_attempts": attempt,
            "plan": state["plan"] + [f"Retrieval attempt {attempt}/2 (hybrid): {len(docs)} chunks retrieved"],
        }

    def grader(self, state):
        kept = state["documents"]
        return {"documents": kept, "plan": state["plan"] + [f"Grading (attempt 1): {len(kept)}/1 chunks relevant"]}

    def rewriter(self, state):
        return {"current_query": "k8s job", "plan": state["plan"] + ["Query rewrite: 'kubernetes job' → 'k8s job'"]}

    def responder(self, state):
        """Streams like the real responder: through token_writer(), only when the run asked for it."""
        self._maybe_fail("responder")
        if not state.get("documents"):
            # Mirrors the real no-evidence reply: fixed text, no LLM, nothing streamed.
            text = "I couldn't find this in the indexed documents."
            return {
                "final_answer": text,
                "status": "No relevant documents found.",
                "messages": [{"role": "assistant", "content": text}],
            }
        emit = token_writer()
        if emit:
            for piece in ANSWER_PIECES:
                emit(piece)
        text = "".join(ANSWER_PIECES)
        message = {"role": "assistant", "content": text}
        return {"final_answer": text, "status": "Response generated.", "messages": [message]}

    def verifier(self, state):
        return {"grounded": True, "plan": state["plan"] + ["Verification: answer supported by sources ✅"]}

    def build(self, **kwargs):
        return build_graph(
            planner=self.planner,
            retriever=self.retriever,
            grader=self.grader,
            rewriter=self.rewriter,
            responder=self.responder,
            verifier=self.verifier,
            **kwargs,
        )


@pytest.fixture
def use_graph(monkeypatch):
    def install(graph):
        monkeypatch.setattr(graph_module, "get_rag_agent", lambda: graph)
        return graph

    return install


def stream(api_app, payload=None):
    response = TestClient(api_app.main.app).post("/query/stream", json=payload or {"q": "How do Jobs work?"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    return parse_sse(response.text)


# ── Happy path ────────────────────────────────────────────────────────────────


def test_event_order_steps_tokens_sources_done(api_app, use_graph):
    use_graph(Nodes().build())

    events = stream(api_app)

    # Verification finishes after the answer, so its step arrives after the tokens.
    assert kinds(events) == ["step", "token", "step", "sources", "done"]
    steps = [data["step"] for kind, data in events if kind == "step"]
    assert steps == [
        "Intent: Technical",
        "Search Term: kubernetes job",
        "Retrieval attempt 1/2 (hybrid): 1 chunks retrieved",
        "Grading (attempt 1): 1/1 chunks relevant",
        "Response generated.",  # responder adds no plan entry → its status is the step
        "Verification: answer supported by sources ✅",
    ]
    assert [data["node"] for kind, data in events if kind == "step"] == [
        "planner",
        "planner",
        "retriever",
        "grader",
        "responder",
        "verifier",
    ]
    # Tokens arrive one by one as the responder emits them (not one final blob).
    assert [data["text"] for kind, data in events if kind == "token"] == ANSWER_PIECES

    sources = events[-2][1]["sources"]
    assert [s["source"] for s in sources] == ["jobs.html"]
    assert events[-1] == (
        "done",
        {
            "status": "Response generated.",
            "grounded": True,
            "thought_process": [
                "Intent: Technical",
                "Search Term: kubernetes job",
                "Retrieval attempt 1/2 (hybrid): 1 chunks retrieved",
                "Grading (attempt 1): 1/1 chunks relevant",
                "Verification: answer supported by sources ✅",
            ],
            "answer": "Jobs run Pods to completion [1].",
        },
    )


def test_stream_and_query_return_the_same_answer(api_app, use_graph):
    """/query (graph.invoke, no token streaming) is unchanged and agrees with the stream."""
    use_graph(Nodes().build())
    client = TestClient(api_app.main.app)

    events = parse_sse(client.post("/query/stream", json={"q": "How do Jobs work?"}).text)
    body = client.post("/query", json={"q": "How do Jobs work?"}).json()

    done = events[-1][1]
    assert body["answer"] == text_of(events) == done["answer"]
    assert body["thought_process"] == done["thought_process"]
    assert body["grounded"] == done["grounded"] is True
    assert body["sources"] == events[-2][1]["sources"]


def test_stream_uses_thread_memory(api_app, use_graph):
    from app.agents.checkpointer import get_checkpointer

    graph = use_graph(Nodes().build(checkpointer=get_checkpointer()))
    client = TestClient(api_app.main.app)

    client.post("/query/stream", json={"q": "first", "thread_id": "t1"})
    client.post("/query/stream", json={"q": "second", "thread_id": "t1"})

    messages = graph.get_state({"configurable": {"thread_id": "t1"}}).values["messages"]
    assert [m["content"] for m in messages if m["role"] == "user"] == ["first", "second"]


# ── Paths without an LLM-streamed answer ──────────────────────────────────────


def test_blocked_by_guardrails_streams_the_canned_reply(api_app, use_graph):
    api_app.verdict = (True, "I can only help with technical documentation questions.")
    use_graph(SimpleNamespace(stream=lambda *a, **k: pytest.fail("agent must not run when guardrails block")))

    events = stream(api_app, {"q": "tell me a joke"})

    assert kinds(events) == ["token", "done"]
    assert text_of(events) == "I can only help with technical documentation questions."
    assert len([e for e in events if e[0] == "token"]) > 1  # rendered progressively
    assert events[-1][1] == {
        "status": "Blocked by guardrails.",
        "grounded": None,
        "thought_process": ["Intent: Guardrails Fired", "Retrieval: Skipped"],
        "answer": "I can only help with technical documentation questions.",
    }


def test_knowledge_base_unavailable(api_app, use_graph):
    use_graph(Nodes(kb_down=True).build())

    events = stream(api_app)

    assert kinds(events) == ["step", "token", "sources", "done"]
    assert text_of(events) == KNOWLEDGE_BASE_UNAVAILABLE_ANSWER
    assert events[-2][1] == {"sources": []}
    assert events[-1][1]["status"] == KNOWLEDGE_BASE_UNAVAILABLE_STATUS
    assert events[-1][1]["grounded"] is None


def test_not_in_the_documents_reply(api_app, use_graph):
    use_graph(Nodes(docs=False).build())

    events = stream(api_app)

    assert kinds(events) == ["step", "token", "sources", "done"]
    steps = [data["step"] for kind, data in events if kind == "step"]
    assert "Query rewrite: 'kubernetes job' → 'k8s job'" in steps  # corrective loop ran
    assert text_of(events) == "I couldn't find this in the indexed documents."
    assert events[-1][1]["status"] == "No relevant documents found."


def test_conversational_answer_is_streamed(api_app, use_graph):
    nodes = Nodes()
    nodes.planner = lambda state: {"current_query": CONVERSATIONAL, "plan": ["Intent: Conversational/Memory"]}
    nodes.responder = lambda state: (
        token_writer()("Hello!") or {"final_answer": "Hello!", "messages": [{"role": "assistant", "content": "Hello!"}]}
    )
    use_graph(nodes.build())

    events = stream(api_app, {"q": "hi"})

    assert kinds(events) == ["step", "token", "step", "sources", "done"]
    assert text_of(events) == "Hello!"  # streamed once, not repeated from final_answer


# ── Errors ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fail_in", ["planner", "responder"])
def test_pipeline_error_becomes_a_friendly_error_event(api_app, use_graph, fail_in):
    use_graph(Nodes(fail_in=fail_in).build())

    response = TestClient(api_app.main.app).post("/query/stream", json={"q": "How do Jobs work?"})
    events = parse_sse(response.text)

    assert events[-1] == ("error", {"message": rag_service.INTERNAL_ERROR_ANSWER})
    assert "done" not in kinds(events)
    assert "secret internal detail" not in response.text
    assert "Traceback" not in response.text


def test_guardrails_init_failure_fails_closed(api_app, use_graph, monkeypatch):
    def broken():
        raise RuntimeError("GROQ_API_KEY missing")

    monkeypatch.setattr(rag_service, "ensure_rails", broken)
    use_graph(SimpleNamespace(stream=lambda *a, **k: pytest.fail("agent must not run unguarded")))

    events = stream(api_app)

    assert events == [("error", {"message": rag_service.INTERNAL_ERROR_ANSWER})]


@pytest.mark.parametrize("payload", [{"q": ""}, {"q": "x" * 4001}, {}])
def test_stream_validates_like_query(api_app, payload):
    assert TestClient(api_app.main.app).post("/query/stream", json=payload).status_code == 422


# ── Real responder node: Portkey stream=True → custom stream writer ───────────


class FakeStream:
    """Mimics the OpenAI Stream object Portkey returns for stream=True."""

    def __init__(self, pieces, cache_status="MISS"):
        self.cache_status = cache_status
        chunks = [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=p))]) for p in pieces]
        chunks.insert(1, SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None))]))
        chunks.append(SimpleNamespace(choices=[]))  # usage-only trailing chunk
        self._chunks = chunks

    def __iter__(self):
        return iter(self._chunks)


@pytest.fixture
def fake_portkey(monkeypatch):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if kwargs.get("stream"):
            return FakeStream(ANSWER_PIECES, cache_status=fake.cache_status)
        message = SimpleNamespace(content="".join(ANSWER_PIECES))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], cache_status=fake.cache_status)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    fake = SimpleNamespace(calls=calls, cache_status="MISS")
    monkeypatch.setitem(
        sys.modules,
        "app.gateway",
        SimpleNamespace(get_portkey_client=lambda: client, extract_cache_status=lambda r: r.cache_status),
    )
    return fake


def _real_responder_graph():
    nodes = Nodes()
    return build_graph(
        planner=nodes.planner,
        retriever=nodes.retriever,
        grader=nodes.grader,
        rewriter=nodes.rewriter,
        responder=responder_module.generate_node,
        verifier=nodes.verifier,
    )


@pytest.mark.parametrize("cache_status, cache_step", [("MISS", False), ("HIT", True)])
def test_real_responder_streams_portkey_tokens(api_app, use_graph, fake_portkey, cache_status, cache_step):
    fake_portkey.cache_status = cache_status
    use_graph(_real_responder_graph())

    events = stream(api_app)

    assert fake_portkey.calls[-1]["stream"] is True
    assert [data["text"] for kind, data in events if kind == "token"] == ANSWER_PIECES
    assert events[-1][1]["answer"] == "".join(ANSWER_PIECES)
    assert ("Cache: Hit ⚡" in events[-1][1]["thought_process"]) is cache_step


def test_real_responder_without_streaming_keeps_the_original_call(api_app, use_graph, fake_portkey):
    use_graph(_real_responder_graph())

    body = TestClient(api_app.main.app).post("/query", json={"q": "How do Jobs work?"}).json()

    assert "stream" not in fake_portkey.calls[-1]  # exactly the pre-streaming call
    assert body["answer"] == "".join(ANSWER_PIECES)


def test_token_writer_is_none_outside_a_graph():
    assert token_writer() is None
