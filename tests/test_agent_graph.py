"""Graph-shape tests: build_graph() wired with FAKE nodes that record their call order.

The routing functions are the real ones, so these tests check the actual edges
and loop bound. No LLM, Qdrant, API key or network is involved.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.agents import graph as graph_module
from app.agents.graph import build_graph, get_rag_agent, self_correction_enabled
from app.agents.routing import CONVERSATIONAL, MAX_RETRIEVAL_ATTEMPTS
from app.agents.state import new_request_state

DOC = {"source": "jobs.html", "source_type": "true", "content": "A Job runs Pods to completion."}


class FakeAgent:
    """Fake node functions. `grades` lists, per grader call, whether the chunk is kept."""

    def __init__(self, *, conversational=False, grades=(True,)):
        self.calls: list[str] = []
        self.conversational = conversational
        self.grades = list(grades)
        self.queries_searched: list[str] = []

    def planner(self, state):
        self.calls.append("planner")
        query = CONVERSATIONAL if self.conversational else "kubernetes job"
        return {"current_query": query, "plan": ["Intent: fake"]}

    def retriever(self, state):
        self.calls.append("retriever")
        self.queries_searched.append(state["current_query"])
        return {"documents": [DOC], "retrieval_attempts": state.get("retrieval_attempts", 0) + 1}

    def grader(self, state):
        self.calls.append("grader")
        keep = self.grades.pop(0) if self.grades else False
        return {"documents": state["documents"] if keep else []}

    def rewriter(self, state):
        self.calls.append("rewriter")
        return {"current_query": f"rewrite of {state['current_query']}"}

    def responder(self, state):
        self.calls.append("responder")
        return {"final_answer": "answer [1]", "messages": [{"role": "assistant", "content": "answer [1]"}]}

    def verifier(self, state):
        self.calls.append("verifier")
        return {"grounded": True}

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


def _run(agent, question="How do Jobs work?", **build_kwargs):
    return agent.build(**build_kwargs).invoke(new_request_state(question))


# a) conversational
def test_conversational_path_skips_retrieval_and_verifier():
    agent = FakeAgent(conversational=True)

    final = _run(agent, "hi")

    assert agent.calls == ["planner", "responder"]
    assert final["grounded"] is None
    assert final["retrieval_attempts"] == 0


# b) happy path
def test_happy_path_grades_answers_and_verifies():
    agent = FakeAgent(grades=[True])

    final = _run(agent)

    assert agent.calls == ["planner", "retriever", "grader", "responder", "verifier"]
    assert final["retrieval_attempts"] == 1
    assert final["documents"] == [DOC]
    assert final["grounded"] is True


# c) corrective path
def test_corrective_path_rewrites_once_then_answers_and_verifies():
    agent = FakeAgent(grades=[False, True])

    final = _run(agent)

    assert agent.calls == [
        "planner",
        "retriever",
        "grader",
        "rewriter",
        "retriever",
        "grader",
        "responder",
        "verifier",
    ]
    assert agent.queries_searched == ["kubernetes job", "rewrite of kubernetes job"]
    assert final["retrieval_attempts"] == 2
    assert final["grounded"] is True


# d) max retries
def test_max_retries_stops_after_two_attempts_without_verifier():
    agent = FakeAgent(grades=[False, False, False, False, False])

    # A low recursion limit turns an accidental infinite loop into a fast failure.
    final = agent.build().invoke(new_request_state("unknowable?"), config={"recursion_limit": 12})

    assert agent.calls == [
        "planner",
        "retriever",
        "grader",
        "rewriter",
        "retriever",
        "grader",
        "responder",
    ]
    assert agent.calls.count("retriever") == MAX_RETRIEVAL_ATTEMPTS
    assert final["retrieval_attempts"] == MAX_RETRIEVAL_ATTEMPTS
    assert final["documents"] == []
    assert final["grounded"] is None


# e) feature flag off
def test_self_correction_false_builds_the_old_linear_graph():
    agent = FakeAgent(grades=[False])  # would trigger a rewrite if the grader were wired in

    final = _run(agent, self_correction=False)

    assert agent.calls == ["planner", "retriever", "responder"]
    assert final["documents"] == [DOC]
    assert final["grounded"] is None


def test_linear_graph_conversational_path():
    agent = FakeAgent(conversational=True)

    _run(agent, "hello", self_correction=False)

    assert agent.calls == ["planner", "responder"]


def test_graph_node_sets_for_both_modes():
    agent = FakeAgent()
    full = set(agent.build().get_graph().nodes)
    linear = set(agent.build(self_correction=False).get_graph().nodes)

    assert {"planner", "retriever", "grader", "rewriter", "responder", "verifier"} <= full
    assert linear.isdisjoint({"grader", "rewriter", "verifier"})


# MemorySaver: per-request reset
def test_new_request_state_resets_attempts_between_requests_on_one_thread():
    """With a checkpointer, an un-reset retrieval_attempts=2 would skip the rewrite on question 2."""
    config = {"configurable": {"thread_id": "same-user"}}
    agent = FakeAgent(grades=[False] * 10)
    compiled = agent.build(checkpointer=MemorySaver())

    compiled.invoke(new_request_state("first question"), config=config)
    first_run = list(agent.calls)
    agent.calls.clear()
    second = compiled.invoke(new_request_state("second question"), config=config)

    assert agent.calls == first_run  # rewrite loop ran again
    assert second["retrieval_attempts"] == MAX_RETRIEVAL_ATTEMPTS
    assert [m["content"] for m in second["messages"] if m["role"] == "user"] == [
        "first question",
        "second question",
    ]  # conversation memory is kept


def test_stale_grounded_does_not_leak_into_a_conversational_turn():
    config = {"configurable": {"thread_id": "t"}}
    technical = FakeAgent(grades=[True])
    checkpointer = MemorySaver()
    technical.build(checkpointer=checkpointer).invoke(new_request_state("how do jobs work"), config=config)

    chat = FakeAgent(conversational=True)
    final = chat.build(checkpointer=checkpointer).invoke(new_request_state("thanks!"), config=config)

    assert final["grounded"] is None
    assert final["documents"] == []


# Feature flag + production factory
@pytest.mark.parametrize(
    "value, expected",
    [
        (None, True),
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("", True),
        ("false", False),
        ("False", False),
        (" 0 ", False),
        ("no", False),
        ("off", False),
    ],
)
def test_self_correction_enabled_parses_env_values(monkeypatch, value, expected):
    monkeypatch.delenv("ENABLE_SELF_CORRECTION", raising=False)
    if value is not None:
        monkeypatch.setenv("ENABLE_SELF_CORRECTION", value)

    assert self_correction_enabled() is expected


@pytest.mark.parametrize("flag, has_grader", [("true", True), ("false", False)])
def test_get_rag_agent_builds_production_graph_without_keys(monkeypatch, flag, has_grader):
    monkeypatch.setenv("ENABLE_SELF_CORRECTION", flag)
    get_rag_agent.cache_clear()
    try:
        agent = get_rag_agent()
        assert get_rag_agent() is agent  # cached
        assert ("grader" in agent.get_graph().nodes) is has_grader
        assert agent.checkpointer is not None  # MemorySaver attached
    finally:
        get_rag_agent.cache_clear()


def test_importing_agent_modules_creates_no_clients():
    """Fresh interpreter: import every app.agents module and build the graph; no gateway/Qdrant import."""
    root = Path(__file__).resolve().parents[1]
    code = """
import sys
from contextlib import nullcontext
from types import SimpleNamespace
sys.modules["logfire"] = SimpleNamespace(span=lambda *a, **k: nullcontext(),
    info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None)
import app.agents.graph as g
import app.agents.nodes.planner, app.agents.nodes.retriever, app.agents.nodes.grader
import app.agents.nodes.rewriter, app.agents.nodes.responder, app.agents.nodes.verifier
g.get_rag_agent()
heavy = [m for m in ("app.gateway", "app.config", "app.services.retrieval.qdrant_service",
         "app.services.retrieval.ranking_service", "portkey_ai", "qdrant_client", "langchain_openai")
         if m in sys.modules]
print(heavy)
"""
    env = {**os.environ, "PYTHONPATH": str(root)}
    for key in ("PORTKEY_API_KEY", "GROQ_API_KEY", "QDRANT_API_KEY", "QDRANT_CLUSTER_ENDPOINT"):
        env.pop(key, None)

    result = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_module_exposes_no_eager_graph():
    assert not hasattr(graph_module, "rag_agent")
