"""The API must import, start and answer GET /health without real secrets.

CI's docker job and the Cloud Run post-deploy check rely on this: they call /health
on a container that has dummy (CI) or freshly mounted (Cloud Run) keys.
"""

import importlib
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.services import rag_service

SECRET_ENV_VARS = (
    "LOGFIRE_TOKEN",
    "GROQ_API_KEY",
    "GROQ_FALLBACK_API_KEY",
    "PORTKEY_API_KEY",
    "GEMINI_API_KEY",
    "QDRANT_API_KEY",
    "QDRANT_CLUSTER_ENDPOINT",
    "LANGSMITH_API_KEY",
)


@pytest.fixture
def fresh_main(monkeypatch):
    """Import app.main from scratch with no secrets and a logfire stub that records configure()."""
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # load_dotenv() must not pull a developer's real .env back in.
    monkeypatch.setitem(sys.modules, "dotenv", SimpleNamespace(load_dotenv=lambda *a, **kw: False))

    configure_calls = []
    fake_logfire = SimpleNamespace(**vars(sys.modules["logfire"])) if "logfire" in sys.modules else SimpleNamespace()
    fake_logfire.configure = lambda *args, **kwargs: configure_calls.append(kwargs)
    monkeypatch.setitem(sys.modules, "logfire", fake_logfire)

    # Same stubs as tests/test_api_query.py: no ingestion stack, no real guardrails.
    monkeypatch.setitem(sys.modules, "app.ingestion.processor", SimpleNamespace(process_file=None))
    monkeypatch.setattr(rag_service, "ensure_rails", lambda: None)
    monkeypatch.delitem(sys.modules, "app.main", raising=False)

    main = importlib.import_module("app.main")
    yield main, configure_calls
    sys.modules.pop("app.main", None)


def test_logfire_only_exports_when_a_token_is_present(fresh_main):
    _, configure_calls = fresh_main

    # Without send_to_logfire="if-token-present", logfire raises LogfireConfigError
    # ("not logged into Logfire") at import time when LOGFIRE_TOKEN is unset.
    assert configure_calls == [{"token": None, "send_to_logfire": "if-token-present"}]


def test_health_after_startup_without_secrets(fresh_main):
    main, _ = fresh_main

    with TestClient(main.app) as client:  # the context manager runs the startup event
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "enterprise-agentic-rag"}


def test_startup_still_rejects_an_invalid_retrieval_mode(fresh_main, monkeypatch):
    main, _ = fresh_main
    monkeypatch.setenv("RETRIEVAL_MODE", "bm25-only")

    with pytest.raises(ValueError, match="RETRIEVAL_MODE"), TestClient(main.app):
        pass
