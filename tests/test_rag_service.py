"""Shared service layer: guardrails → agent flow, search, and document listing (all mocked)."""

import importlib
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from app.agents import graph as graph_module
from app.services import rag_service

# ── list_documents ────────────────────────────────────────────────────────────


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


def test_list_documents_reads_processed_data_folder(tmp_path):
    _write(
        tmp_path / "true" / "cronjobs.docx.json",
        {"filename": "cronjobs.docx", "source_type": "true", "chunks": ["a", "b", "c"]},
    )
    _write(
        tmp_path / "uploaded" / "Notes.pdf.json", {"filename": "Notes.pdf", "source_type": "uploaded", "chunks": ["x"]}
    )
    _write(
        tmp_path / "true" / "architecture.pptx.json",
        {"filename": "architecture.pptx", "source_type": "true", "chunks": []},
    )
    _write(tmp_path / "noisy" / "legacy.txt.json", {"chunks": ["1", "2"]})  # no filename/source_type
    _write(tmp_path / "noisy" / "broken.json", "{not json")  # skipped
    _write(tmp_path / "noisy" / "list.json", ["not", "a", "dict"])  # skipped
    _write(tmp_path / "noisy" / "weird.json", {"filename": "weird.txt", "chunks": "nope"})
    _write(tmp_path / "top-level.json", {"filename": "ignored"})  # not in a source folder
    _write(tmp_path / "true" / "readme.md", "not json")  # not *.json

    documents = rag_service.list_documents(tmp_path)

    assert documents == [
        {"filename": "legacy.txt", "source_type": "noisy", "chunk_count": 2},
        {"filename": "weird.txt", "source_type": "noisy", "chunk_count": 0},
        {"filename": "architecture.pptx", "source_type": "true", "chunk_count": 0},
        {"filename": "cronjobs.docx", "source_type": "true", "chunk_count": 3},
        {"filename": "Notes.pdf", "source_type": "uploaded", "chunk_count": 1},
    ]


def test_list_documents_missing_folder_is_empty(tmp_path):
    assert rag_service.list_documents(tmp_path / "does-not-exist") == []


def test_list_documents_default_folder_honours_env_override(tmp_path, monkeypatch):
    _write(tmp_path / "true" / "a.txt.json", {"filename": "a.txt", "source_type": "true", "chunks": ["1"]})
    monkeypatch.setenv("PROCESSED_DATA_DIR", str(tmp_path))

    assert rag_service.list_documents() == [{"filename": "a.txt", "source_type": "true", "chunk_count": 1}]


def test_default_folder_is_resolved_from_project_root_not_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("PROCESSED_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    assert rag_service.processed_data_dir() == rag_service.PROJECT_ROOT / "processed_data"
    assert (rag_service.PROJECT_ROOT / "app" / "services" / "rag_service.py").is_file()


# ── answer_question ───────────────────────────────────────────────────────────


class FakeAgent:
    def __init__(self, output=None, error=None):
        self.output, self.error, self.calls = output, error, []

    def invoke(self, state, config):
        self.calls.append((state, config))
        if self.error:
            raise self.error
        return self.output


@pytest.fixture
def guardrails(monkeypatch):
    """Fake app.guardrails: records ensure_rails() calls and returns a configurable verdict."""
    fake = SimpleNamespace(init_calls=0, verdict=(False, None), init_error=None)

    def ensure_rails():
        fake.init_calls += 1
        if fake.init_error:
            raise fake.init_error

    fake.ensure_rails = ensure_rails
    fake.guard = lambda message: fake.verdict
    monkeypatch.setitem(sys.modules, "app.guardrails", fake)
    return fake


def _use_agent(monkeypatch, agent):
    monkeypatch.setattr(graph_module, "get_rag_agent", lambda: agent)
    return agent


def test_answer_question_runs_guardrails_then_agent(monkeypatch, guardrails):
    agent = _use_agent(
        monkeypatch,
        FakeAgent(
            output={
                "final_answer": "Jobs run Pods to completion [1].",
                "plan": ["Intent: Technical", "Verification: answer supported by sources ✅"],
                "status": "Response generated.",
                "documents": [
                    {"source": "jobs.html", "source_type": "true", "content": "A Job runs Pods.", "score": 0.8}
                ],
                "grounded": True,
            }
        ),
    )

    result = rag_service.answer_question("What is a Job?", "thread-9")

    assert guardrails.init_calls == 1
    state, config = agent.calls[0]
    assert config == {"configurable": {"thread_id": "thread-9"}}
    assert state["retrieval_attempts"] == 0 and state["grounded"] is None  # per-request reset
    assert result == {
        "question": "What is a Job?",
        "answer": "Jobs run Pods to completion [1].",
        "thought_process": ["Intent: Technical", "Verification: answer supported by sources ✅"],
        "status": "Response generated.",
        "sources": [
            {
                "source": "jobs.html",
                "source_type": "true",
                "content": "A Job runs Pods.",
                "preview": "A Job runs Pods.",
                "retrieval_score": 0.8,
                "rerank_score": None,
            }
        ],
        "grounded": True,
        "query_id": None,  # analytics is off in tests unless a test opts in
    }


def test_answer_question_blocked_by_guardrails_skips_agent(monkeypatch, guardrails):
    guardrails.verdict = (True, "I can't help with that — but ask me anything technical!")
    agent = _use_agent(monkeypatch, FakeAgent(output={}))

    result = rag_service.answer_question("tell me a joke", "t")

    assert agent.calls == []
    assert result == {
        "question": "tell me a joke",
        "answer": "I can't help with that — but ask me anything technical!",
        "thought_process": ["Intent: Guardrails Fired", "Retrieval: Skipped"],
        "status": "Blocked by guardrails.",
        "sources": [],
        "grounded": None,
        "query_id": None,
    }


def test_answer_question_agent_error_returns_error_shape(monkeypatch, guardrails):
    _use_agent(monkeypatch, FakeAgent(error=RuntimeError("groq down")))

    result = rag_service.answer_question("What is a Job?", "t")

    assert result == {
        "question": "What is a Job?",
        "answer": rag_service.INTERNAL_ERROR_ANSWER,
        "thought_process": ["Error encountered during execution."],
        "status": "error",
        "sources": [],
        "grounded": None,
        "query_id": None,
    }


def test_guardrails_init_failure_fails_closed(monkeypatch, guardrails):
    """If guardrails can't start, the question must NOT reach the agent unguarded."""
    guardrails.init_error = RuntimeError("GROQ_API_KEY missing")
    agent = _use_agent(monkeypatch, FakeAgent(output={"final_answer": "unguarded!"}))

    result = rag_service.answer_question("What is a Job?", "t")

    assert agent.calls == []
    assert result["status"] == "error"


# ── search_knowledge ──────────────────────────────────────────────────────────


def test_search_knowledge_searches_reranks_and_formats_without_llm(monkeypatch):
    calls = {}
    candidates = [
        {"source": f"d{i}.txt", "source_type": "true", "content": f"chunk {i}", "score": 0.5} for i in range(30)
    ]

    def search(query, limit):
        calls["search"] = (query, limit)
        return candidates[:limit]

    def rerank(query, records, top_n):
        calls["rerank"] = (query, len(records), top_n)
        return [dict(r, rerank_score=0.9) for r in records[:top_n]]

    monkeypatch.setitem(
        sys.modules, "app.services.retrieval.qdrant_service", SimpleNamespace(search_enterprise_knowledge=search)
    )
    monkeypatch.setitem(sys.modules, "app.services.retrieval.ranking_service", SimpleNamespace(rerank_records=rerank))
    monkeypatch.setitem(sys.modules, "app.gateway", None)  # any LLM import would raise

    results = rag_service.search_knowledge("cron schedule", top_k=8)

    assert calls["search"] == ("cron schedule", 24)  # over-fetch 3x (min 15) for the reranker
    assert calls["rerank"] == ("cron schedule", 24, 8)
    assert len(results) == 8
    assert results[0] == {
        "source": "d0.txt",
        "source_type": "true",
        "content": "chunk 0",
        "preview": "chunk 0",
        "retrieval_score": 0.5,
        "rerank_score": 0.9,
    }


@pytest.mark.parametrize("top_k, limit", [(1, 15), (5, 15), (20, 60)])
def test_search_knowledge_candidate_pool(monkeypatch, top_k, limit):
    seen = {}
    monkeypatch.setitem(
        sys.modules,
        "app.services.retrieval.qdrant_service",
        SimpleNamespace(search_enterprise_knowledge=lambda q, limit: seen.update(limit=limit) or []),
    )
    monkeypatch.setitem(
        sys.modules,
        "app.services.retrieval.ranking_service",
        SimpleNamespace(rerank_records=lambda q, records, top_n: records),
    )

    assert rag_service.search_knowledge("q", top_k) == []
    assert seen["limit"] == limit


# ── guardrails ensure_rails (real module, heavy deps stubbed) ─────────────────


def test_ensure_rails_initialises_once_even_with_concurrent_callers(monkeypatch):
    built = []

    class SlowLLMRails:
        def __init__(self, config, llm):
            time.sleep(0.05)  # widen the race window
            built.append(self)

    monkeypatch.setitem(sys.modules, "langchain_groq", SimpleNamespace(ChatGroq=lambda **kwargs: object()))
    monkeypatch.setitem(
        sys.modules,
        "nemoguardrails",
        SimpleNamespace(RailsConfig=SimpleNamespace(from_content=lambda **kwargs: object()), LLMRails=SlowLLMRails),
    )
    monkeypatch.setitem(sys.modules, "app.config", SimpleNamespace(settings=SimpleNamespace(GROQ_API_KEY="")))
    monkeypatch.delitem(sys.modules, "app.guardrails.rails", raising=False)
    monkeypatch.delitem(sys.modules, "app.guardrails", raising=False)
    rails = importlib.import_module("app.guardrails.rails")

    threads = [threading.Thread(target=rails.ensure_rails) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    rails.ensure_rails()

    assert len(built) == 1
    assert rails._rails is built[0]

    # Leave no half-stubbed guardrails module behind for later tests.
    sys.modules.pop("app.guardrails.rails", None)
    sys.modules.pop("app.guardrails", None)
