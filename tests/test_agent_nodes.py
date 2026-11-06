"""Node tests with fake LLMs / services: no API keys, no network."""

import sys
from types import SimpleNamespace

import pytest

from app.agents.nodes import grader, responder, retriever, rewriter, verifier
from app.agents.state import new_request_state

DOCS = [
    {"source": "jobs.html", "content": "A Job runs Pods until completion."},
    {"source": "intel.pdf", "content": "SR-IOV exposes virtual functions."},
    {"source": "cron.docx", "content": "CronJobs create Jobs on a schedule."},
]


class FakeLLM:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.prompts = reply, error, []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return SimpleNamespace(content=self.reply)


def _use_llm(monkeypatch, module, llm):
    monkeypatch.setattr(module, "get_llm", lambda feature: llm)
    return llm


def _technical_state(question="How do CronJobs work?", **extra):
    state = new_request_state(question)
    state.update(current_query="kubernetes cronjob", retrieval_attempts=1, plan=["Intent: Technical"])
    state.update(extra)
    return state


# ── grader ────────────────────────────────────────────────────────────────────


def test_grader_keeps_only_relevant_chunks_in_one_batched_call(monkeypatch):
    llm = _use_llm(monkeypatch, grader, FakeLLM('{"relevant": [0, 2]}'))

    update = grader.grade_node(_technical_state(documents=DOCS))

    assert update["documents"] == [DOCS[0], DOCS[2]]
    assert update["plan"][-1] == "Grading (attempt 1): 2/3 chunks relevant"
    assert len(llm.prompts) == 1  # batched, not one call per chunk
    assert all(f"[{i}]" in llm.prompts[0] for i in range(3))
    assert "How do CronJobs work?" in llm.prompts[0]


def test_grader_empty_verdict_drops_all_chunks(monkeypatch):
    _use_llm(monkeypatch, grader, FakeLLM('{"relevant": []}'))

    update = grader.grade_node(_technical_state(documents=DOCS))

    assert update["documents"] == []
    assert update["plan"][-1] == "Grading (attempt 1): 0/3 chunks relevant"


@pytest.mark.parametrize("llm", [FakeLLM("chunk 0 looks good"), FakeLLM(error=TimeoutError("gateway down"))])
def test_grader_fails_open_on_unreadable_output_or_error(monkeypatch, llm):
    _use_llm(monkeypatch, grader, llm)

    update = grader.grade_node(_technical_state(documents=DOCS))

    assert update["documents"] == DOCS
    assert "fail open" in update["plan"][-1]


def test_grader_skips_llm_when_nothing_was_retrieved(monkeypatch):
    llm = _use_llm(monkeypatch, grader, FakeLLM('{"relevant": [0]}'))

    update = grader.grade_node(_technical_state(documents=[]))

    assert update["documents"] == []
    assert llm.prompts == []


# ── rewriter ──────────────────────────────────────────────────────────────────


def test_rewriter_sets_new_query_and_logs_the_step(monkeypatch):
    llm = _use_llm(monkeypatch, rewriter, FakeLLM("Kubernetes CronJob schedule syntax"))

    update = rewriter.rewrite_node(_technical_state())

    assert update["current_query"] == "Kubernetes CronJob schedule syntax"
    assert update["plan"][-1] == "Query rewrite: 'kubernetes cronjob' → 'Kubernetes CronJob schedule syntax'"
    assert "kubernetes cronjob" in llm.prompts[0]


@pytest.mark.parametrize("llm", [FakeLLM(""), FakeLLM("kubernetes cronjob"), FakeLLM(error=RuntimeError("x"))])
def test_rewriter_falls_back_to_original_question(monkeypatch, llm):
    _use_llm(monkeypatch, rewriter, llm)

    update = rewriter.rewrite_node(_technical_state())

    assert update["current_query"] == "How do CronJobs work?"
    assert update["plan"][-1].endswith("(using the original question)")


# ── verifier ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reply, grounded, step",
    [
        ('{"grounded": true}', True, "Verification: answer supported by sources ✅"),
        ('{"grounded": false}', False, "Verification: answer not fully supported by sources ⚠️"),
        ("It depends.", None, "Verification: inconclusive"),
    ],
)
def test_verifier_stores_grounded(monkeypatch, reply, grounded, step):
    llm = _use_llm(monkeypatch, verifier, FakeLLM(reply))

    update = verifier.verify_node(_technical_state(documents=DOCS, final_answer="CronJobs create Jobs [3]."))

    assert update["grounded"] is grounded
    assert update["plan"][-1] == step
    assert "CronJobs create Jobs [3]." in llm.prompts[0]
    assert "[3] Source: cron.docx" in llm.prompts[0]


def test_verifier_error_is_inconclusive(monkeypatch):
    _use_llm(monkeypatch, verifier, FakeLLM(error=ConnectionError("down")))

    update = verifier.verify_node(_technical_state(documents=DOCS, final_answer="x"))

    assert update["grounded"] is None


# ── responder ─────────────────────────────────────────────────────────────────


def test_responder_with_no_documents_refuses_without_calling_the_llm(monkeypatch):
    def explode():
        raise AssertionError("LLM must not be called when there is no evidence")

    monkeypatch.setitem(
        sys.modules,
        "app.gateway",
        SimpleNamespace(get_portkey_client=explode, extract_cache_status=lambda r: "MISS"),
    )

    update = responder.generate_node(_technical_state(documents=[], retrieval_attempts=2))

    assert "indexed documents" in update["final_answer"]
    assert "upload" in update["final_answer"].lower()
    assert "How do CronJobs work?" in update["final_answer"]
    assert update["grounded"] is None
    assert update["plan"][-1] == "Answer: indexed documents don't cover this question"
    assert update["messages"] == [{"role": "assistant", "content": update["final_answer"]}]


def test_responder_prompt_numbers_sources_and_requires_inline_citations():
    prompt = responder.build_technical_prompt(DOCS, history="", question="How do CronJobs work?")

    assert "[1] Source: jobs.html" in prompt
    assert "[3] Source: cron.docx" in prompt
    assert "like [1]" in prompt
    assert "ONLY" in prompt


# ── retriever ─────────────────────────────────────────────────────────────────


def test_retriever_increments_attempts_and_logs_the_step(monkeypatch):
    searched = []
    monkeypatch.delenv("RETRIEVAL_MODE", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "app.services.retrieval.qdrant_service",
        SimpleNamespace(search_enterprise_knowledge=lambda q, limit, mode: searched.append((q, mode)) or DOCS),
    )
    monkeypatch.setitem(
        sys.modules,
        "app.services.retrieval.ranking_service",
        SimpleNamespace(rerank_records=lambda q, records, top_n: records[:top_n]),
    )
    state = _technical_state(retrieval_attempts=1, current_query="cronjob schedule syntax")

    update = retriever.retrieve_node(state)

    assert searched == [("cronjob schedule syntax", "hybrid")]
    assert update["retrieval_attempts"] == 2
    assert update["documents"] == DOCS
    assert update["plan"][-1] == "Retrieval attempt 2/2 (hybrid): 3 chunks retrieved for 'cronjob schedule syntax'"
