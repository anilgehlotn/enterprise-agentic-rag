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
