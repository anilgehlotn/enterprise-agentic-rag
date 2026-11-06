"""Qdrant query construction, collection schema and error handling (client and embedders mocked)."""

from types import SimpleNamespace

import pytest
from qdrant_client.http import models

from app.services.retrieval import qdrant_service, schema
from app.services.retrieval.schema import CollectionSchemaError, RetrievalError

DENSE = [0.1, 0.2, 0.3]
SPARSE = models.SparseVector(indices=[7, 42], values=[1.0, 1.0])


def _new_schema_info(modifier=models.Modifier.IDF):
    return SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(
                vectors={"dense": models.VectorParams(size=3, distance=models.Distance.COSINE)},
                sparse_vectors={"bm25": models.SparseVectorParams(modifier=modifier)},
            )
        )
    )


def _old_schema_info():
    # What get_collection() returns for a collection created with an unnamed vector.
    return SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(
                vectors=models.VectorParams(size=3, distance=models.Distance.COSINE),
                sparse_vectors=None,
            )
        )
    )


class FakeClient:
    def __init__(self, info=None, exists=True, query_error=None):
        self.info, self.exists, self.query_error = info or _new_schema_info(), exists, query_error
        self.queries, self.created, self.deleted = [], [], []

    def collection_exists(self, name):
        return self.exists

    def get_collection(self, name):
        return self.info

    def create_collection(self, collection_name, **kwargs):
        self.created.append((collection_name, kwargs))
        self.exists = True

    def delete_collection(self, name):
        self.deleted.append(name)
        self.exists = False

    def query_points(self, **kwargs):
        self.queries.append(kwargs)
        if self.query_error:
            raise self.query_error
        point = SimpleNamespace(
            score=0.5, payload={"text": "backoffLimit: 4", "source": "jobs.html", "source_type": "true"}
        )
        return SimpleNamespace(points=[point])


@pytest.fixture
def client(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(qdrant_service, "get_qdrant_client", lambda: fake)
    monkeypatch.setattr(qdrant_service, "embed_query", lambda query: DENSE)
    monkeypatch.setattr(qdrant_service, "embed_sparse_query", lambda query: SPARSE)
    monkeypatch.setattr(qdrant_service, "get_embedding_dim", lambda: 3)
    monkeypatch.setattr(qdrant_service, "_verified_collections", set())
    monkeypatch.delenv("RETRIEVAL_MODE", raising=False)
    return fake


# ── query construction ────────────────────────────────────────────────────────


def test_hybrid_prefetches_dense_and_bm25_and_fuses_with_rrf(client):
    results = qdrant_service.search_enterprise_knowledge("backoffLimit", limit=15, mode="hybrid")

    (query,) = client.queries
    assert query["collection_name"] == qdrant_service.settings.QDRANT_COLLECTION
    assert query["query"] == models.FusionQuery(fusion=models.Fusion.RRF)
    assert query["limit"] == 15
    dense_prefetch, sparse_prefetch = query["prefetch"]
    assert (dense_prefetch.using, dense_prefetch.query) == ("dense", DENSE)
    assert (sparse_prefetch.using, sparse_prefetch.query) == ("bm25", SPARSE)
    assert dense_prefetch.limit == sparse_prefetch.limit == qdrant_service.HYBRID_PREFETCH_LIMIT
    assert "using" not in query
    assert results == [
        {
            "content": "backoffLimit: 4",
            "source": "jobs.html",
            "source_type": "true",
            "score": 0.5,
            "retrieval_mode": "hybrid",
        }
    ]


def test_hybrid_prefetch_is_never_smaller_than_the_limit(client):
    qdrant_service.search_enterprise_knowledge("q", limit=60, mode="hybrid")

    assert [p.limit for p in client.queries[0]["prefetch"]] == [60, 60]


def test_default_mode_is_hybrid(client):
    qdrant_service.search_enterprise_knowledge("q", limit=5)

    assert "prefetch" in client.queries[0]


def test_dense_mode_queries_the_dense_vector_only(client, monkeypatch):
    monkeypatch.setattr(
        qdrant_service, "embed_sparse_query", lambda q: pytest.fail("dense mode must not compute a BM25 vector")
    )
    monkeypatch.setenv("RETRIEVAL_MODE", "dense")

    results = qdrant_service.search_enterprise_knowledge("q", limit=8)

    (query,) = client.queries
    assert query["using"] == "dense"
    assert query["query"] == DENSE
    assert query["limit"] == 8
    assert "prefetch" not in query
    assert results[0]["retrieval_mode"] == "dense"
    assert set(results[0]) == {"content", "source", "source_type", "score", "retrieval_mode"}


@pytest.mark.parametrize("value, expected", [("hybrid", "hybrid"), ("DENSE", "dense"), (" Hybrid ", "hybrid")])
def test_retrieval_mode_env_values(monkeypatch, value, expected):
    monkeypatch.setenv("RETRIEVAL_MODE", value)
    assert schema.resolve_retrieval_mode() == expected


def test_empty_retrieval_mode_falls_back_to_hybrid(monkeypatch):
    monkeypatch.setenv("RETRIEVAL_MODE", "")
    assert schema.resolve_retrieval_mode() == "hybrid"


@pytest.mark.parametrize("value", ["sparse", "bm25", "hybird"])
def test_invalid_retrieval_mode_raises_a_clear_error(client, monkeypatch, value):
    monkeypatch.setenv("RETRIEVAL_MODE", value)

    with pytest.raises(ValueError, match=rf"Invalid RETRIEVAL_MODE='{value}': expected one of hybrid, dense"):
        qdrant_service.search_enterprise_knowledge("q")
    assert client.queries == []


# ── failures raise RetrievalError instead of returning [] ─────────────────────


def test_qdrant_failure_raises_retrieval_error_not_empty_list(client):
    client.query_error = ConnectionError("connection refused")

    with pytest.raises(RetrievalError) as error:
        qdrant_service.search_enterprise_knowledge("q", mode="hybrid")
    assert isinstance(error.value.__cause__, ConnectionError)


def test_embedding_failure_raises_retrieval_error(client, monkeypatch):
    def broken(query):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(qdrant_service, "embed_query", broken)

    with pytest.raises(RetrievalError):
        qdrant_service.search_enterprise_knowledge("q", mode="dense")


def test_no_matches_is_still_an_empty_list(client, monkeypatch):
    monkeypatch.setattr(client, "query_points", lambda **kwargs: SimpleNamespace(points=[]))

    assert qdrant_service.search_enterprise_knowledge("q") == []


# ── collection schema ─────────────────────────────────────────────────────────


def test_ensure_collection_creates_named_dense_and_bm25_idf_vectors(client):
    client.exists = False

    qdrant_service.ensure_collection()

    ((name, kwargs),) = client.created
    assert name == qdrant_service.settings.QDRANT_COLLECTION
    assert kwargs["vectors_config"] == {"dense": models.VectorParams(size=3, distance=models.Distance.COSINE)}
    assert kwargs["sparse_vectors_config"] == {"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)}


def test_ensure_collection_accepts_existing_new_schema_without_recreating(client):
    qdrant_service.ensure_collection()

    assert client.created == []


def test_old_unnamed_vector_collection_raises_reingest_error(client):
    client.info = _old_schema_info()

    with pytest.raises(CollectionSchemaError) as error:
        qdrant_service.ensure_collection()

    message = str(error.value)
    assert message.startswith(
        "Collection uses the old schema — re-ingest with: python -m app.ingestion.processor DATA --wipe"
    )
    assert client.created == []  # never silently mixes formats


@pytest.mark.parametrize(
    "info",
    [
        SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors={"text": models.VectorParams(size=3, distance=models.Distance.COSINE)},
                    sparse_vectors={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
                )
            )
        ),
        SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors={"dense": models.VectorParams(size=3, distance=models.Distance.COSINE)}, sparse_vectors=None
                )
            )
        ),
        _new_schema_info(modifier=models.Modifier.NONE),
    ],
)
def test_partial_or_wrong_schemas_are_rejected(info):
    with pytest.raises(CollectionSchemaError, match="re-ingest with"):
        qdrant_service.check_collection_schema(info, "enterprise_rag")


def test_search_on_old_schema_raises_the_reingest_error(client):
    client.info = _old_schema_info()

    with pytest.raises(CollectionSchemaError, match="re-ingest with"):
        qdrant_service.search_enterprise_knowledge("q")
    assert client.queries == []


def test_schema_error_is_a_retrieval_error():
    # So the agent and the MCP tool report an old-schema collection as "unavailable".
    assert issubclass(CollectionSchemaError, RetrievalError)


def test_schema_is_checked_once_per_process(client, monkeypatch):
    checks = []
    original = client.get_collection
    monkeypatch.setattr(client, "get_collection", lambda name: checks.append(name) or original(name))

    qdrant_service.search_enterprise_knowledge("a")
    qdrant_service.search_enterprise_knowledge("b")

    assert len(checks) == 1
    assert len(client.queries) == 2


def test_delete_collection_forgets_the_verified_schema(client):
    qdrant_service.ensure_collection()
    assert qdrant_service.delete_collection() is True
    assert client.deleted == [qdrant_service.settings.QDRANT_COLLECTION]
    assert qdrant_service._verified_collections == set()
    assert qdrant_service.delete_collection() is False
