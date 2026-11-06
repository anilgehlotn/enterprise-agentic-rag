"""Small dependency stubs keep pure unit tests independent of cloud services."""

import importlib
import os
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

# Unit tests never trace to LangSmith. app.config (imported by the retrieval and ingestion
# tests) loads .env and would otherwise set LANGCHAIN_TRACING_V2=true for every later
# LangGraph test, sending runs over the network. load_dotenv() doesn't override these.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
# The app defaults to a SQLite checkpointer at ./data/checkpoints.sqlite. Tests use the
# in-process one unless a test opts into sqlite with a temp file (monkeypatch.setenv).
os.environ["CHECKPOINTER"] = "memory"


if "logfire" not in sys.modules:
    sys.modules["logfire"] = SimpleNamespace(
        configure=lambda *args, **kwargs: None,
        span=lambda *args, **kwargs: nullcontext(),
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )


@pytest.fixture(autouse=True)
def _fresh_process_state():
    """
    The rate limiter and the checkpointer are process-wide singletons. Reset them
    around every test so request counts and conversations never leak between tests.
    Only touches modules that are already imported (importing them here could
    change what other tests see).
    """
    _reset_singletons()
    yield
    _reset_singletons()


def _reset_singletons():
    rate_limit = sys.modules.get("app.rate_limit")
    if rate_limit is not None:
        rate_limit.limiter.reset()
    graph = sys.modules.get("app.agents.graph")
    if graph is not None:
        graph.get_rag_agent.cache_clear()
    checkpointer = sys.modules.get("app.agents.checkpointer")
    if checkpointer is not None:
        checkpointer.close_checkpointer()


@pytest.fixture
def api_app(monkeypatch):
    """
    Fresh app.main with no ingestion stack and fake guardrails (no keys, no network).

    guardrails.verdict is what guard() returns: (False, None) lets the question through,
    (True, "reply") blocks it. ensure_calls counts guardrail warm-ups.
    """
    from app.services import rag_service

    state = SimpleNamespace(ensure_calls=0, verdict=(False, None))

    def ensure_rails():
        state.ensure_calls += 1

    # app.main imports the ingestion pipeline (Qdrant, Gemini embeddings) at module level.
    monkeypatch.setitem(sys.modules, "app.ingestion.processor", SimpleNamespace(process_file=None))
    monkeypatch.setitem(sys.modules, "app.guardrails", SimpleNamespace(guard=lambda message: state.verdict))
    monkeypatch.setattr(rag_service, "ensure_rails", ensure_rails)
    monkeypatch.delitem(sys.modules, "app.main", raising=False)
    state.main = importlib.import_module("app.main")
    yield state
    sys.modules.pop("app.main", None)
