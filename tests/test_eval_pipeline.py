import sys

import pytest

from evals import pipeline
from evals.pipeline import extract_context_texts, run_pipeline


def _api_sources():
    """Shape of /query `sources` as produced by build_citations()."""
    return [
        {
            "source": f"doc{i}.txt",
            "source_type": "true",
            "content": f"chunk {i} " + "y" * 400,
            "preview": f"chunk {i}",
            "retrieval_score": 0.5,
            "rerank_score": 0.9,
        }
        for i in range(7)
    ]


def test_importing_pipeline_does_not_import_ragas():
    assert "ragas" not in sys.modules
    assert "evals.metrics" not in sys.modules


def test_extract_context_texts_returns_content_strings():
    texts = extract_context_texts(_api_sources())

    assert len(texts) == 5
    assert all(isinstance(t, str) for t in texts)
    assert texts[0].startswith("chunk 0 ")


def test_extract_context_texts_skips_empty_and_accepts_strings():
    sources = [{"content": ""}, {"source": "no-content"}, "plain string", None, {"content": "real"}]

    assert extract_context_texts(sources) == ["plain string", "real"]


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_run_pipeline_stores_text_contexts_that_metrics_can_slice(monkeypatch):
    payload = {
        "answer": "CronJobs run Jobs on a schedule.",
        "thought_process": ["Intent: Technical", "Search Term: cronjob"],
        "sources": _api_sources(),
    }
    monkeypatch.setattr(pipeline.requests, "post", lambda *a, **kw: _FakeResponse(payload))
    monkeypatch.setattr(pipeline, "DELAY_BETWEEN_CALLS", 0)
    golden = {"rag_samples": [{"question": "What is a CronJob?"}, {"question": "What is a Job?"}]}

    result = run_pipeline(golden)

    for sample in result["rag_samples"]:
        contexts = sample["actual_contexts"]
        assert len(contexts) == 5
        assert all(isinstance(c, str) for c in contexts)
        # Same operation evals.metrics applies (c[:CONTEXT_TRUNCATE]); crashed on dicts before.
        truncated = [c[:300] for c in contexts[:2]]
        assert all(len(c) == 300 for c in truncated)
        assert sample["actual_tools_called"] == ["retrieve_documents"]
    # The input dataset must not be mutated.
    assert "actual_contexts" not in golden["rag_samples"][0]


@pytest.mark.parametrize("sources", [[], None])
def test_run_pipeline_handles_no_sources(monkeypatch, sources):
    payload = {"answer": "Hi!", "thought_process": ["Intent: Conversational/Memory"], "sources": sources}
    monkeypatch.setattr(pipeline.requests, "post", lambda *a, **kw: _FakeResponse(payload))
    monkeypatch.setattr(pipeline, "DELAY_BETWEEN_CALLS", 0)

    result = run_pipeline({"rag_samples": [{"question": "hello"}]})

    assert result["rag_samples"][0]["actual_contexts"] == []


# ── detect_tool with the self-correcting agent's thought_process ─────────────

from evals.pipeline import detect_tool


@pytest.mark.parametrize(
    "thought_process, expected",
    [
        # happy path
        (["Intent: Technical", "Search Term: cronjob", "Retrieval attempt 1/2: 5 chunks retrieved for 'cronjob'",
          "Grading (attempt 1): 3/5 chunks relevant", "Verification: answer supported by sources ✅"],
         "retrieve_documents"),
        # corrective path whose rewritten query mentions "memory"
        (["Intent: Technical", "Search Term: pod limits", "Retrieval attempt 1/2: 5 chunks retrieved for 'pod limits'",
          "Grading (attempt 1): 0/5 chunks relevant",
          "Query rewrite: 'pod limits' → 'kubernetes container memory limits'",
          "Retrieval attempt 2/2: 5 chunks retrieved for 'kubernetes container memory limits'",
          "Grading (attempt 2): 0/5 chunks relevant", "Answer: indexed documents don't cover this question"],
         "retrieve_documents"),
        # retrieval steps alone are enough
        (["Retrieval attempt 1/2: 0 chunks retrieved for 'x'"], "retrieve_documents"),
        # conversational (+ cache hit)
        (["Intent: Conversational/Memory", "Retrieval: Skipped", "Cache: Hit ⚡"], "direct_answer"),
        (["Intent: Guardrails Fired", "Retrieval: Skipped"], "guardrails"),
        (["Error encountered during execution."], "unknown"),
    ],
)
def test_detect_tool_classifies_self_correcting_plans(thought_process, expected):
    assert detect_tool(thought_process) == expected
