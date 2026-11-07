"""evals/chunking_experiment.py end to end on a tiny corpus: fake embedders, real in-memory Qdrant.

No API keys, no network, no model downloads: dense and BM25 vectors are hashed bags of
words and the reranker keeps the hybrid order.
"""

import hashlib
import re

import pytest
from qdrant_client.http import models

from app.ingestion.chunking.config import ChunkingConfig
from evals import chunking_experiment as ce

DIM = 64
WORD = re.compile(r"\w+")

TRUE_DOC = (
    "Redis work queue.\n\n"
    "Start a temporary interactive pod for running the Redis CLI, connect to Redis and fill the queue "
    "named job2 with rpush job2 apple, rpush job2 banana and rpush job2 cherry.\n\n"
    "Horizontal Pod Autoscaling scales the number of pods in a deployment based on observed CPU "
    "utilization. Install the Metrics Server before you create the autoscaler.\n\n"
    "A CronJob runs Jobs on a repeating schedule. Set concurrencyPolicy to Forbid so that runs never overlap."
)
NOISY_DOC = (
    "Huffman coding builds a minimum redundancy prefix code from symbol frequencies.\n\n"
    "The Burrows Wheeler transform sorts all rotations of a block before compression.\n\n"
    "Cache coherence protocols keep the private caches of many cores consistent."
)
SAMPLES = [
    {
        "id": 1,
        "question": "How do you fill the Redis queue job2 with rpush?",
        "relevant_contexts": [
            "Start a temporary interactive pod for running the Redis CLI, connect to Redis and fill the queue "
            "named job2 with rpush job2 apple, rpush job2 banana and rpush job2 cherry."
        ],
    },
    {
        "id": 2,
        "question": "What does Horizontal Pod Autoscaling scale based on CPU utilization?",
        "relevant_contexts": [
            "Horizontal Pod Autoscaling scales the number of pods in a deployment based on observed CPU utilization."
        ],
    },
]


def _bucket(word: str, size: int) -> int:
    return int(hashlib.md5(word.encode()).hexdigest(), 16) % size


def fake_dense(texts):
    vectors = []
    for text in texts:
        vector = [0.0] * DIM
        for word in WORD.findall(text.lower()):
            vector[_bucket(word, DIM)] += 1.0
        vector[0] += 1e-3  # never all zeros
        vectors.append(vector)
    return vectors


def fake_sparse(texts):
    vectors = []
    for text in texts:
        counts: dict[int, float] = {}
        for word in WORD.findall(text.lower()):
            index = _bucket(word, 10_000)
            counts[index] = counts.get(index, 0.0) + 1.0
        vectors.append(models.SparseVector(indices=list(counts), values=list(counts.values())))
    return vectors


def keep_order(question, records, top_n):
    return records[:top_n]


@pytest.fixture
def corpus_dirs(tmp_path):
    true_dir, noisy_dir = tmp_path / "true_data", tmp_path / "noisy_data"
    true_dir.mkdir()
    noisy_dir.mkdir()
    (true_dir / "jobs.txt").write_text(TRUE_DOC, encoding="utf-8")
    (noisy_dir / "compression.txt").write_text(NOISY_DOC, encoding="utf-8")
    (noisy_dir / "skipped.csv").write_text("a,b", encoding="utf-8")
    (noisy_dir / "subfolder").mkdir()
    return ((true_dir, "true"), (noisy_dir, "noisy"))


@pytest.fixture
def no_real_qdrant(monkeypatch):
    """The experiment must never touch the shared (Qdrant Cloud) client."""
    from app.services.retrieval import qdrant_service

    def forbidden():
        raise AssertionError("the chunking experiment used the real Qdrant client")

    monkeypatch.setattr(qdrant_service, "get_qdrant_client", forbidden)


def make_embedders():
    dense = ce.CachedEmbedder("fake-bow", DIM, fake_dense, lambda text: fake_dense([text])[0])
    sparse = ce.CachedSparse(fake_sparse, lambda text: fake_sparse([text])[0])
    return dense, sparse


# ── Grid, corpus, cache ───────────────────────────────────────────────────────


def test_full_grid_matches_the_spec():
    grid = ce.build_grid()

    assert len(grid) == 18
    assert {c.chunk_size for c in grid} == {500, 1000, 1500}
    for config in grid:
        if config.strategy in ("paragraph", "semantic"):
            assert config.chunk_overlap == 0
    assert ChunkingConfig("fixed", 1000, 150) in grid and ChunkingConfig("fixed", 1000, 0) in grid
    assert ChunkingConfig("recursive", 1500, 225) in grid
    assert ce.CURRENT_DEFAULT in grid


def test_quick_grid_is_smaller():
    grid = ce.build_grid(quick=True)

    assert len(grid) == 8
    assert {c.chunk_size for c in grid} == {500, 1000}
    assert ChunkingConfig("recursive", 500, 75) in grid


def test_load_corpus_parses_supported_files_with_their_source_type(corpus_dirs):
    documents = ce.load_corpus(corpus_dirs)

    assert [(d.filename, d.source_type) for d in documents] == [("jobs.txt", "true"), ("compression.txt", "noisy")]
    assert documents[0].text == TRUE_DOC


def test_cached_embedder_embeds_each_unique_text_once():
    calls = []

    def embed(texts):
        calls.append(list(texts))
        return fake_dense(texts)

    dense = ce.CachedEmbedder("fake", DIM, embed, lambda text: fake_dense([text])[0])

    first = dense.documents(["a b", "c d", "a b"])
    second = dense.documents(["c d", "e f"])

    assert calls == [["a b", "c d"], ["e f"]]
    assert first[0] == first[2] and second[0] == first[1]
    assert dense.computed_texts == 3
    assert dense.requested_chars == 5 * 3  # every request counts toward the cold-cache cost


def test_cached_embedder_rejects_a_wrong_vector_count():
    dense = ce.CachedEmbedder("broken", DIM, lambda texts: [], lambda text: [])

    with pytest.raises(RuntimeError, match="0 vectors for 1 texts"):
        dense.documents(["text"])


# ── End to end ────────────────────────────────────────────────────────────────


def test_experiment_runs_end_to_end_on_in_memory_qdrant(corpus_dirs, no_real_qdrant):
    documents = ce.load_corpus(corpus_dirs)
    dense, sparse = make_embedders()
    configs = [
        ChunkingConfig("paragraph", 400, 0),
        ChunkingConfig("fixed", 120, 18),
        ChunkingConfig("recursive", 120, 18),
        ChunkingConfig("semantic", 200, 0),
    ]

    results = ce.run_experiment(configs, documents, SAMPLES, dense, sparse, keep_order, k=3, candidates=6)

    assert [r.config for r in results] == configs
    for result in results:
        summary = result.summary(k=3)
        assert result.n_chunks > 0 and 0 < result.avg_chunk_len <= result.config.chunk_size
        assert len(result.ranks) == len(result.strict_ranks) == len(result.coverages) == len(SAMPLES)
        assert summary["hit_rate"] == 1.0, result.config  # easy questions: the answer chunk is retrieved
        assert 0.0 <= summary["noise_rate"] <= 1.0
        assert 0 < summary["context_chars"] <= 3 * result.config.chunk_size
        assert result.embed_chars >= sum(len(d.text) for d in documents) * 0.9
    # Fixed 120-char windows hold only part of the long golden context: the partial rule still
    # counts the hit, the strict production rule doesn't.
    fixed = results[1]
    assert fixed.ranks[0] is not None and fixed.strict_ranks[0] is None


def test_index_stores_chunking_metadata_in_the_payload(corpus_dirs):
    from qdrant_client import QdrantClient

    documents = ce.load_corpus(corpus_dirs)
    dense, sparse = make_embedders()
    config = ChunkingConfig("fixed", 120, 18)
    chunks = ce._chunk_corpus(config, documents, dense)
    client = QdrantClient(":memory:")

    ce._index(client, chunks, config, dense, sparse)

    points, _ = client.scroll(ce.COLLECTION, limit=1000, with_payload=True)
    assert len(points) == len(chunks)
    payload = next(p.payload for p in points if p.payload["source"] == "jobs.txt" and p.payload["chunk_index"] == 1)
    assert payload["chunking_strategy"] == "fixed"
    assert (payload["chunk_size"], payload["chunk_overlap"], payload["source_type"]) == (120, 18, "true")
    client.close()


def test_main_writes_the_report(corpus_dirs, tmp_path, monkeypatch, no_real_qdrant):
    import json

    dataset = tmp_path / "golden.json"
    dataset.write_text(json.dumps({"rag_samples": SAMPLES}), encoding="utf-8")
    output = tmp_path / "results" / "chunking.md"
    monkeypatch.setattr(ce, "DEFAULT_CORPUS", corpus_dirs)
    dense, sparse = make_embedders()
    monkeypatch.setattr(ce, "local_embedder", lambda: dense)
    monkeypatch.setattr(ce, "CachedSparse", lambda: sparse)
    monkeypatch.setattr(ce, "flashrank_rerank", keep_order)
    monkeypatch.setattr(ce, "build_grid", lambda quick: [ChunkingConfig("fixed", 120, 18), ce.CURRENT_DEFAULT])

    ce.main(["--quick", "--dataset", str(dataset), "--output", str(output), "--k", "3", "--candidates", "6"])

    report = output.read_text(encoding="utf-8")
    assert "# Chunking experiment" in report
    assert "fake-bow" in report and "absolute values will differ" in report
    assert "paragraph (current default)" in report
    assert "## Effect of overlap" in report and "## Findings" in report
    assert ce.FINDINGS_PLACEHOLDER in report
    assert "python -m evals.chunking_experiment --quick --embedder local" in report


# ── Statistics + report helpers ──────────────────────────────────────────────


def test_paired_bootstrap_ci():
    assert ce.paired_bootstrap_ci([0.5] * 15, [0.5] * 15) == (0.0, 0.0)
    low, high = ce.paired_bootstrap_ci([1.0] * 15, [0.0] * 14 + [1.0])
    assert 0 < low <= high <= 1.0
    with pytest.raises(ValueError):
        ce.paired_bootstrap_ci([1.0], [])


def _result(strategy, size, overlap, ranks, context_chars=1000):
    result = ce.ConfigResult(ChunkingConfig(strategy, size, overlap), n_chunks=10, avg_chunk_len=size * 0.9)
    result.ranks = list(ranks)
    result.strict_ranks = list(ranks)
    result.coverages = [1.0 if r else 0.0 for r in ranks]
    result.source_types = [["true"] for _ in ranks]
    result.context_chars = [context_chars for _ in ranks]
    return result


def test_rank_results_sorts_by_mrr_then_hit_rate_then_cost():
    a = _result("fixed", 500, 0, [1, 1, None])
    b = _result("fixed", 1000, 0, [1, 1, 1])
    c = _result("recursive", 500, 0, [1, 1, 1], context_chars=500)

    assert [r.config.label for r in ce.rank_results([a, b, c])] == [c.config.label, b.config.label, a.config.label]


def test_results_table_marks_best_noise_and_default():
    best = _result("recursive", 500, 75, [1] * 15)
    close = _result("fixed", 500, 0, [1] * 14 + [2])
    default = _result("paragraph", 1500, 0, [None] * 10 + [1] * 5)

    table = ce.results_table([default, close, best])

    lines = table.splitlines()
    assert "| 1 | recursive | 500 | 75 (15%) |" in lines[2] and "best" in lines[2]
    assert "within noise" in lines[3]
    assert "paragraph (current default)" in lines[4] and "worse" in lines[4]


def test_overlap_table_pairs_configs_with_and_without_overlap():
    table = ce.overlap_table([_result("fixed", 500, 0, [1, None]), _result("fixed", 500, 75, [1, 1])])

    assert "| fixed | 500 | 0.500 → 1.000 | 0.50 → 1.00 |" in table
    assert ce.overlap_table([_result("semantic", 500, 0, [1])]) == "_No overlap pairs in this grid._"
