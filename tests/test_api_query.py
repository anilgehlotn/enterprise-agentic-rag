"""/query must behave exactly as before the rag_service refactor (answer_question mocked)."""

import importlib
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.services import rag_service

RESPONSE_KEYS = {"question", "answer", "thought_process", "status", "sources", "grounded"}


@pytest.fixture
def api(monkeypatch):
    # app.main imports the ingestion pipeline (Qdrant, Gemini embeddings) at module level;
    # stub it so the API can be imported in CI without those packages or keys.
    monkeypatch.setitem(sys.modules, "app.ingestion.processor", SimpleNamespace(process_file=None))
    monkeypatch.delitem(sys.modules, "app.main", raising=False)
    calls = SimpleNamespace(answer=[], ensure=0)

    def ensure_rails():
        calls.ensure += 1

    monkeypatch.setattr(rag_service, "ensure_rails", ensure_rails)
    main = importlib.import_module("app.main")
    yield main, calls
    sys.modules.pop("app.main", None)


def _mock_answer(monkeypatch, calls, result):
    def answer_question(question, thread_id):
        calls.answer.append((question, thread_id))
        return result

    monkeypatch.setattr(rag_service, "answer_question", answer_question)


def test_query_returns_the_same_response_shape(api, monkeypatch):
    main, calls = api
    _mock_answer(monkeypatch, calls, {
        "question": "What is a CronJob?",
        "answer": "CronJobs create Jobs on a schedule [1].",
        "thought_process": ["Intent: Technical", "Search Term: cronjob"],
        "status": "Response generated.",
        "sources": [{"source": "cronjobs.docx", "source_type": "true", "content": "CronJobs create Jobs.",
                     "preview": "CronJobs create Jobs.", "retrieval_score": 0.7, "rerank_score": 0.9}],
        "grounded": True,
    })

    response = TestClient(main.app).post("/query", json={"q": "What is a CronJob?", "thread_id": "abc"})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == RESPONSE_KEYS
    assert body == {
        "question": "What is a CronJob?",
        "answer": "CronJobs create Jobs on a schedule [1].",
        "thought_process": ["Intent: Technical", "Search Term: cronjob"],
        "status": "Response generated.",
        "sources": [{"source": "cronjobs.docx", "source_type": "true", "content": "CronJobs create Jobs.",
                     "preview": "CronJobs create Jobs.", "retrieval_score": 0.7, "rerank_score": 0.9}],
        "grounded": True,
    }
    assert calls.answer == [("What is a CronJob?", "abc")]


def test_query_default_thread_id_is_unchanged(api, monkeypatch):
    main, calls = api
    _mock_answer(monkeypatch, calls, {"question": "hi", "answer": "Hello!", "thought_process": [],
                                      "status": "ok", "sources": [], "grounded": None})

    TestClient(main.app).post("/query", json={"q": "hi"})

    assert calls.answer == [("hi", "default_user")]


@pytest.mark.parametrize("result", [
    # Guardrails block and pipeline error, exactly as the endpoint used to build them.
    {"question": "joke", "answer": "I can't help with that.", "status": "Blocked by guardrails.",
     "thought_process": ["Intent: Guardrails Fired", "Retrieval: Skipped"], "sources": [], "grounded": None},
    {"question": "x", "answer": rag_service.INTERNAL_ERROR_ANSWER, "status": "error",
     "thought_process": ["Error encountered during execution."], "sources": [], "grounded": None},
])
def test_query_blocked_and_error_shapes(api, monkeypatch, result):
    main, calls = api
    _mock_answer(monkeypatch, calls, result)

    body = TestClient(main.app).post("/query", json={"q": result["question"]}).json()

    assert body == result


@pytest.mark.parametrize("payload", [{"q": ""}, {"q": "x" * 4001}, {}])
def test_query_validation_is_unchanged(api, monkeypatch, payload):
    main, calls = api
    _mock_answer(monkeypatch, calls, {})

    response = TestClient(main.app).post("/query", json=payload)

    assert response.status_code == 422
    assert calls.answer == []


def test_startup_warms_guardrails_through_the_service(api):
    main, calls = api

    with TestClient(main.app):
        pass

    assert calls.ensure == 1


def test_health_endpoint_still_works(api):
    main, _ = api

    assert TestClient(main.app).get("/health").json() == {"status": "ok", "service": "enterprise-agentic-rag"}
