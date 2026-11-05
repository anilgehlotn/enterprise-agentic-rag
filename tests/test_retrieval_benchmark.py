"""Benchmark harness with fake search / rerank: no Qdrant, Gemini or FlashRank."""

import pytest

from evals import retrieval_benchmark as bench
from app.services.retrieval.schema import RetrievalError

GOLDEN = "spec: parallelism: 2 template: metadata: name: job-wq-2"
SAMPLES = [
    {"id": 1, "question": "parallelism?", "relevant_contexts": [GOLDEN]},
    {"id": 2, "question": "unknowable?", "relevant_contexts": ["nothing in the corpus says this at all"]},
]


def _rec(content, source_type="true"):
    return {"content": content, "source": "x", "source_type": source_type, "score": 0.1}


def fake_search(query, limit, mode):
    noisy = [_rec(f"noise {i}", "noisy") for i in range(3)]
    relevant = _rec("intro " + GOLDEN)
    if query == "parallelism?":
        # dense ranks the relevant chunk 4th, hybrid ranks it 1st
        records = noisy + [relevant] if mode == "dense" else [relevant] + noisy
    else:
        records = noisy
    return records[:limit]


def fake_rerank(query, records, top_n):
    # Moves the relevant chunk to the top, keeps the others in order.
    return sorted(records, key=lambda r: GOLDEN not in r["content"])[:top_n]


def test_run_benchmark_scores_all_four_configurations():
    results, per_question = bench.run_benchmark(SAMPLES, k=5, candidates=15, delay=0,
                                                search=fake_search, rerank=fake_rerank)

    summary = {key: result.summary(5) for key, result in results.items()}
    assert summary["dense"]["hit_rate"] == 0.5
    assert summary["dense"]["mrr"] == pytest.approx((1 / 4 + 0) / 2)
    assert summary["dense_rerank"]["mrr"] == pytest.approx((1 + 0) / 2)
    assert summary["hybrid"]["mrr"] == pytest.approx((1 + 0) / 2)
    assert summary["dense"]["noise_rate"] == pytest.approx(6 / 7)
    assert per_question[0] == {"id": 1, "question": "parallelism?", "dense": 4, "dense_rerank": 1,
                               "hybrid": 1, "hybrid_rerank": 1}
    assert per_question[1]["hybrid"] is None


def test_report_contains_table_and_metadata():
    results, per_question = bench.run_benchmark(SAMPLES, delay=0, search=fake_search, rerank=fake_rerank)
    meta = {"date": "2026-11-06 10:00 UTC", "collection": "enterprise_rag", "points": 80,
            "dense_model": "models/gemini-embedding-2-preview", "sparse_model": "Qdrant/bm25"}

    report = bench.render_report(results, per_question, meta, k=5, candidates=15, n_questions=2)

    assert "2026-11-06 10:00 UTC" in report and "(80 chunks)" in report
    assert "models/gemini-embedding-2-preview" in report
    for label in ("Dense only", "Dense + FlashRank", "Hybrid (RRF)", "Hybrid + FlashRank"):
        assert f"| {label} |" in report
    assert "| 1 | parallelism? | 4 | 1 | 1 | 1 |" in report


def test_search_retries_transient_failures(monkeypatch):
    monkeypatch.setattr(bench, "BACKOFF_SECONDS", 0)
    attempts = []

    def flaky(query, limit, mode):
        attempts.append(1)
        if len(attempts) < 3:
            raise RetrievalError("429")
        return [_rec("ok")]

    records, latency_ms = bench._search_with_retry(flaky, "q", "hybrid", 15)

    assert len(attempts) == 3 and records == [_rec("ok")] and latency_ms >= 0


def test_golden_dataset_loads_15_questions():
    samples = bench.load_samples(bench.DEFAULT_DATASET)
    assert len(samples) == 15
    assert all(s["relevant_contexts"] for s in samples)
