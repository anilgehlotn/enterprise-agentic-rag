"""Knowledge base outage (RetrievalError) → a clear "unavailable" result, never "no results".

Covers the retriever node, both graph variants (no rewrite loop), /query, the shared
rag_service search and the MCP tools. Qdrant, LLMs and guardrails are all faked.
"""

import importlib
import sys
from types import SimpleNamespace

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from app import mcp_server
from app.agents import graph as graph_module
from app.agents.graph import build_graph
from app.agents.nodes import retriever
from app.agents.state import new_request_state
from app.services import rag_service
from app.services.retrieval.schema import (
    KNOWLEDGE_BASE_UNAVAILABLE_ANSWER,
    KNOWLEDGE_BASE_UNAVAILABLE_STATUS,
    CollectionSchemaError,
    RetrievalError,
)


@pytest.fixture
def qdrant_down(monkeypatch):
    """Stub retrieval modules: every search raises RetrievalError, reranking must not run."""
    searches = []

    def search(query, limit, mode=None):
        searches.append((query, mode))
        raise RetrievalError("Knowledge base search failed (ConnectionError).")

    monkeypatch.delenv("RETRIEVAL_MODE", raising=False)
    monkeypatch.setitem(
        sys.modules, "app.services.retrieval.qdrant_service", SimpleNamespace(search_enterprise_knowledge=search)
    )
    monkeypatch.setitem(
        sys.modules,
        "app.services.retrieval.ranking_service",
        SimpleNamespace(rerank_records=lambda *a, **k: pytest.fail("rerank must not run")),
    )
    return searches


def _technical_planner(state):
    return {"current_query": "job backoffLimit", "plan": state["plan"] + ["Intent: Technical"]}


def _must_not_run(name):
    def node(state):
        raise AssertionError(f"{name} must not run when the knowledge base is unavailable")

    return node


def _graph(self_correction=True):
    return build_graph(
        planner=_technical_planner,
        retriever=retriever.retrieve_node,  # the real node
        grader=_must_not_run("grader"),
        rewriter=_must_not_run("rewriter"),
        responder=_must_not_run("responder"),
        verifier=_must_not_run("verifier"),
        self_correction=self_correction,
    )


# ── retriever node ────────────────────────────────────────────────────────────


def test_retriever_node_reports_unavailable(qdrant_down):
    state = new_request_state("What does backoffLimit do?")
    state.update(current_query="job backoffLimit", plan=["Intent: Technical"])

    update = retriever.retrieve_node(state)

    assert update["documents"] == []
    assert update["retrieval_attempts"] == 1
    assert update["retrieval_error"]
    assert update["status"] == KNOWLEDGE_BASE_UNAVAILABLE_STATUS
    assert update["final_answer"] == KNOWLEDGE_BASE_UNAVAILABLE_ANSWER
    assert update["grounded"] is None
    assert update["plan"][-1] == "Retrieval attempt 1/2 (hybrid): knowledge base unavailable"
    assert update["messages"] == [{"role": "assistant", "content": KNOWLEDGE_BASE_UNAVAILABLE_ANSWER}]
    assert qdrant_down == [("job backoffLimit", "hybrid")]


def test_old_schema_collection_is_also_reported_unavailable(monkeypatch):
    def search(query, limit, mode=None):
        raise CollectionSchemaError("'enterprise_rag' has a single unnamed vector")

    monkeypatch.setitem(
        sys.modules, "app.services.retrieval.qdrant_service", SimpleNamespace(search_enterprise_knowledge=search)
    )
    monkeypatch.setitem(
        sys.modules,
        "app.services.retrieval.ranking_service",
        SimpleNamespace(rerank_records=lambda *a, **k: pytest.fail("rerank must not run")),
    )
    state = new_request_state("q")
    state.update(plan=["Intent: Technical"])

    update = retriever.retrieve_node(state)

    assert update["status"] == KNOWLEDGE_BASE_UNAVAILABLE_STATUS
    assert "re-ingest with" in update["retrieval_error"]


@pytest.mark.parametrize("self_correction", [True, False])
def test_graph_ends_after_one_attempt_without_rewrite(qdrant_down, self_correction):
    final = _graph(self_correction).invoke(new_request_state("What does backoffLimit do?"))

    assert final["final_answer"] == KNOWLEDGE_BASE_UNAVAILABLE_ANSWER
    assert final["status"] == KNOWLEDGE_BASE_UNAVAILABLE_STATUS
    assert final["retrieval_attempts"] == 1
    assert len(qdrant_down) == 1  # no rewrite-and-retry
    assert final["plan"][-1] == "Retrieval attempt 1/2 (hybrid): knowledge base unavailable"


def test_retrieval_error_is_reset_for_the_next_request_on_the_same_thread():
    assert new_request_state("next question")["retrieval_error"] is None


# ── /query ────────────────────────────────────────────────────────────────────


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setitem(sys.modules, "app.ingestion.processor", SimpleNamespace(process_file=None))
    monkeypatch.delitem(sys.modules, "app.main", raising=False)
    monkeypatch.setattr(rag_service, "ensure_rails", lambda: None)
    monkeypatch.setitem(
        sys.modules, "app.guardrails", SimpleNamespace(ensure_rails=lambda: None, guard=lambda message: (False, None))
    )
    main = importlib.import_module("app.main")
    yield main
    sys.modules.pop("app.main", None)


def test_query_returns_normal_shape_with_unavailable_status(api, monkeypatch, qdrant_down):
    monkeypatch.setattr(graph_module, "get_rag_agent", lambda: _graph())

    response = TestClient(api.app).post("/query", json={"q": "What does backoffLimit do?", "thread_id": "t1"})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"question", "answer", "thought_process", "status", "sources", "grounded"}
    assert body["question"] == "What does backoffLimit do?"
    assert body["answer"] == KNOWLEDGE_BASE_UNAVAILABLE_ANSWER
    assert body["status"] == KNOWLEDGE_BASE_UNAVAILABLE_STATUS
    assert body["sources"] == []
    assert body["grounded"] is None
    assert "Retrieval attempt 1/2 (hybrid): knowledge base unavailable" in body["thought_process"]


def test_startup_rejects_invalid_retrieval_mode(api, monkeypatch):
    monkeypatch.setenv("RETRIEVAL_MODE", "sparse")

    with pytest.raises(ValueError, match="Invalid RETRIEVAL_MODE"):
        with TestClient(api.app):
            pass


# ── rag_service search + MCP tools ────────────────────────────────────────────


def test_search_knowledge_propagates_retrieval_error(qdrant_down):
    with pytest.raises(RetrievalError):
        rag_service.search_knowledge("backoffLimit", top_k=5)


def _call(name, arguments):
    async def go():
        async with Client(mcp_server.mcp) as client:
            return await client.call_tool(name, arguments)

    return anyio.run(go)


def test_mcp_search_returns_friendly_unavailable_error(monkeypatch):
    def down(query, top_k):
        raise RetrievalError("Knowledge base search failed (ConnectError) at 10.0.0.5:6333")

    monkeypatch.setattr(rag_service, "search_knowledge", down)

    result = _call("search_knowledge_base", {"query": "backoffLimit"})

    assert result.is_error is True
    text = result.content[0].text
    assert "temporarily unavailable" in text
    assert "not an empty result" in text
    assert "10.0.0.5" not in text and "Traceback" not in text


def test_mcp_search_end_to_end_through_the_service(qdrant_down):
    result = _call("search_knowledge_base", {"query": "backoffLimit", "top_k": 3})

    assert result.is_error is True
    assert "temporarily unavailable" in result.content[0].text


def test_mcp_ask_reports_unavailable_status_as_a_normal_answer(monkeypatch):
    monkeypatch.setattr(
        rag_service,
        "answer_question",
        lambda question, thread_id: {
            "question": question,
            "answer": KNOWLEDGE_BASE_UNAVAILABLE_ANSWER,
            "thought_process": ["Intent: Technical", "Retrieval attempt 1/2 (hybrid): knowledge base unavailable"],
            "status": KNOWLEDGE_BASE_UNAVAILABLE_STATUS,
            "sources": [],
            "grounded": None,
        },
    )

    result = _call("ask_knowledge_base", {"question": "What does backoffLimit do?"})

    assert result.is_error is False
    assert result.structured_content["status"] == KNOWLEDGE_BASE_UNAVAILABLE_STATUS
    assert result.structured_content["answer"] == KNOWLEDGE_BASE_UNAVAILABLE_ANSWER
