"""FlashRank reranking with a fake flashrank package (no ONNX model, no download).

Covers the FLASHRANK_CACHE_DIR setting (the Docker image bakes the model there) and
the rerank paths, including the fall-back-to-original-order behaviour on failure.
"""

import importlib
import sys
from types import SimpleNamespace

import pytest


class FakeRequest:
    def __init__(self, query, passages):
        self.query = query
        self.passages = passages


class FakeRanker:
    """Scores a passage by how many query words it contains (highest first)."""

    created: list = []
    fail_init_with_cache_dir = False
    fail_rerank = False

    def __init__(self, cache_dir=None, **kwargs):
        if cache_dir is not None and FakeRanker.fail_init_with_cache_dir:
            raise PermissionError("cache dir not writable")
        FakeRanker.created.append(cache_dir)

    def rerank(self, request):
        if FakeRanker.fail_rerank:
            raise RuntimeError("onnx failure")
        words = set(request.query.lower().split())
        scored = [{**p, "score": float(sum(w in p["text"].lower() for w in words))} for p in request.passages]
        return sorted(scored, key=lambda p: p["score"], reverse=True)


@pytest.fixture
def ranking(monkeypatch):
    FakeRanker.created = []
    FakeRanker.fail_init_with_cache_dir = False
    FakeRanker.fail_rerank = False
    monkeypatch.setitem(sys.modules, "flashrank", SimpleNamespace(Ranker=FakeRanker, RerankRequest=FakeRequest))
    monkeypatch.delitem(sys.modules, "app.services.retrieval.ranking_service", raising=False)
    module = importlib.import_module("app.services.retrieval.ranking_service")
    yield module
    sys.modules.pop("app.services.retrieval.ranking_service", None)


# ── Cache dir ──────────────────────────────────────────────────────────────────


def test_default_cache_dir_is_unchanged(ranking, monkeypatch):
    monkeypatch.delenv("FLASHRANK_CACHE_DIR", raising=False)

    ranking._get_ranker()

    assert ranking.flashrank_cache_dir() == "/tmp/flashrank"
    assert FakeRanker.created == ["/tmp/flashrank"]


def test_env_var_sets_cache_dir(ranking, monkeypatch):
    monkeypatch.setenv("FLASHRANK_CACHE_DIR", "/opt/models")

    ranking._get_ranker()

    assert FakeRanker.created == ["/opt/models"]


def test_empty_env_var_falls_back_to_default(ranking, monkeypatch):
    monkeypatch.setenv("FLASHRANK_CACHE_DIR", "")

    assert ranking.flashrank_cache_dir() == "/tmp/flashrank"


def test_ranker_is_created_once(ranking, monkeypatch):
    monkeypatch.setenv("FLASHRANK_CACHE_DIR", "/opt/models")

    assert ranking._get_ranker() is ranking._get_ranker()
    assert len(FakeRanker.created) == 1


def test_unusable_cache_dir_falls_back_to_flashrank_default(ranking):
    FakeRanker.fail_init_with_cache_dir = True

    ranking._get_ranker()

    assert FakeRanker.created == [None]


# ── Reranking ──────────────────────────────────────────────────────────────────

RECORDS = [
    {"content": "pods and nodes", "source": "a.txt", "score": 0.9},
    {"content": "kubernetes cronjob schedule", "source": "b.txt", "score": 0.5},
    {"content": "", "source": "empty.txt", "score": 0.4},
    {"content": "cronjob basics", "source": "c.txt", "score": 0.3},
]


def test_rerank_records_orders_by_score_and_keeps_metadata(ranking):
    ranked = ranking.rerank_records("kubernetes cronjob schedule", RECORDS, top_n=2)

    assert [r["source"] for r in ranked] == ["b.txt", "c.txt"]
    assert ranked[0]["rerank_score"] == 3.0
    assert ranked[0]["score"] == 0.5  # retrieval score retained
    assert "rerank_score" not in RECORDS[1]  # input records are not mutated


def test_rerank_records_empty_input(ranking):
    assert ranking.rerank_records("q", []) == []


def test_rerank_records_falls_back_to_retrieval_order_on_failure(ranking):
    FakeRanker.fail_rerank = True

    assert ranking.rerank_records("cronjob", RECORDS, top_n=2) == RECORDS[:2]


def test_rerank_documents_orders_texts(ranking):
    docs = ["pods", "cronjob schedule", "cronjob"]

    assert ranking.rerank_documents("cronjob schedule", docs, top_n=2) == ["cronjob schedule", "cronjob"]


def test_rerank_documents_empty_and_failure(ranking):
    assert ranking.rerank_documents("q", []) == []
    FakeRanker.fail_rerank = True
    assert ranking.rerank_documents("q", ["a", "b", "c"], top_n=2) == ["a", "b"]
