"""Qdrant access: the shared client, the collection schema, and hybrid / dense search.

Collection schema (one collection serves both retrieval modes):
    vectors        "dense"  Gemini embedding, cosine, dim = get_embedding_dim()
    sparse vectors "bm25"   fastembed Qdrant/bm25 term weights, Modifier.IDF

Retrieval (RETRIEVAL_MODE, default hybrid):
    hybrid  prefetch dense top-K + BM25 top-K, fused server-side with Qdrant's RRF
    dense   dense-only nearest-neighbour query on "dense"

Every record has the same shape in both modes: content, source, source_type, score,
retrieval_mode. In hybrid mode `score` is the RRF fusion score (rank-based, not a
cosine similarity); FlashRank reranks either way.
"""

from functools import lru_cache

import logfire
from qdrant_client import QdrantClient
from qdrant_client.http import models

from app.config import settings
from app.services.retrieval.embedding import embed_query, get_embedding_dim
from app.services.retrieval.schema import (
    DENSE_VECTOR,
    HYBRID,
    SPARSE_VECTOR,
    CollectionSchemaError,
    RetrievalError,
    resolve_retrieval_mode,
)
from app.services.retrieval.sparse_embedding import embed_sparse_query

# Candidates fetched from EACH of dense and BM25 before fusion (never fewer than `limit`).
HYBRID_PREFETCH_LIMIT = 30

# Collections whose schema was already checked in this process (avoids a round trip per query).
_verified_collections: set[str] = set()


@lru_cache(maxsize=1)
def get_qdrant_client() -> QdrantClient:
    """The one Qdrant client for retrieval AND ingestion, created on first use (not at import)."""
    return QdrantClient(url=settings.QDRANT_URL, api_key=settings.QDRANT_API_KEY)


# ── Collection schema ─────────────────────────────────────────────────────────

def collection_schema(dim: int) -> dict:
    """create_collection() arguments for the named dense + BM25 sparse layout."""
    return {
        "vectors_config": {
            DENSE_VECTOR: models.VectorParams(size=dim, distance=models.Distance.COSINE),
        },
        "sparse_vectors_config": {
            SPARSE_VECTOR: models.SparseVectorParams(modifier=models.Modifier.IDF),
        },
    }


def check_collection_schema(info, collection_name: str) -> None:
    """Raises CollectionSchemaError unless the collection has named "dense" + "bm25" (IDF) vectors."""
    params = info.config.params
    vectors = params.vectors
    if not isinstance(vectors, dict):
        raise CollectionSchemaError(f"'{collection_name}' has a single unnamed vector")
    if DENSE_VECTOR not in vectors:
        raise CollectionSchemaError(f"'{collection_name}' has no '{DENSE_VECTOR}' vector")

    sparse = params.sparse_vectors or {}
    if SPARSE_VECTOR not in sparse:
        raise CollectionSchemaError(f"'{collection_name}' has no '{SPARSE_VECTOR}' sparse vector")
    if sparse[SPARSE_VECTOR].modifier != models.Modifier.IDF:
        raise CollectionSchemaError(f"'{collection_name}' sparse vector '{SPARSE_VECTOR}' lacks the IDF modifier")


def ensure_collection(client: QdrantClient | None = None, collection_name: str | None = None) -> None:
    """
    The single place a collection is created. Creates it when missing; otherwise
    verifies its schema and raises CollectionSchemaError for the old unnamed-vector
    format, so old and new points are never mixed.
    """
    client = client or get_qdrant_client()
    name = collection_name or settings.QDRANT_COLLECTION

    if client.collection_exists(name):
        check_collection_schema(client.get_collection(name), name)
        _verified_collections.add(name)
        return

    dim = get_embedding_dim()
    client.create_collection(collection_name=name, **collection_schema(dim))
    _verified_collections.add(name)
    logfire.info(
        f"Created collection '{name}': '{DENSE_VECTOR}' ({dim}-dim, Cosine) + '{SPARSE_VECTOR}' (sparse, IDF)."
    )


def delete_collection(client: QdrantClient | None = None, collection_name: str | None = None) -> bool:
    """Drops the collection if it exists (used by --wipe). Returns True if one was deleted."""
    client = client or get_qdrant_client()
    name = collection_name or settings.QDRANT_COLLECTION
    _verified_collections.discard(name)
    if not client.collection_exists(name):
        return False
    client.delete_collection(name)
    return True


def _verify_schema_once(client: QdrantClient, name: str) -> None:
    if name in _verified_collections:
        return
    check_collection_schema(client.get_collection(name), name)
    _verified_collections.add(name)


# ── Search ────────────────────────────────────────────────────────────────────

def build_query_request(dense_vector, sparse_vector, mode: str, limit: int) -> dict:
    """query_points() keyword arguments (without collection_name) for the given mode."""
    if mode == HYBRID:
        prefetch_limit = max(limit, HYBRID_PREFETCH_LIMIT)
        return {
            "prefetch": [
                models.Prefetch(query=dense_vector, using=DENSE_VECTOR, limit=prefetch_limit),
                models.Prefetch(query=sparse_vector, using=SPARSE_VECTOR, limit=prefetch_limit),
            ],
            "query": models.FusionQuery(fusion=models.Fusion.RRF),
            "limit": limit,
            "with_payload": True,
        }
    return {"query": dense_vector, "using": DENSE_VECTOR, "limit": limit, "with_payload": True}


def search_enterprise_knowledge(query: str, limit: int = 8, mode: str | None = None) -> list[dict]:
    """
    Searches the knowledge base in `mode` (default: RETRIEVAL_MODE env var, hybrid).

    Returns [] only when nothing matched. Raises RetrievalError when Qdrant or the
    embedding service fails (CollectionSchemaError for an old-schema collection),
    and ValueError for an invalid mode.
    """
    mode = resolve_retrieval_mode(mode)
    name = settings.QDRANT_COLLECTION

    try:
        client = get_qdrant_client()
        _verify_schema_once(client, name)
        dense_vector = embed_query(query)
        sparse_vector = embed_sparse_query(query) if mode == HYBRID else None
        response = client.query_points(
            collection_name=name, **build_query_request(dense_vector, sparse_vector, mode, limit)
        )
    except CollectionSchemaError as exc:
        logfire.error(f"❌ Qdrant schema mismatch: {exc}")
        raise
    except Exception as exc:
        logfire.error(f"❌ Qdrant {mode} search failed: {exc}")
        raise RetrievalError(f"Knowledge base search failed ({type(exc).__name__}).") from exc

    return [
        {
            "content": point.payload.get("text", ""),
            "source": point.payload.get("source", "Unknown"),
            "source_type": point.payload.get("source_type", "unknown"),
            "score": point.score,
            "retrieval_mode": mode,
        }
        for point in response.points
    ]
