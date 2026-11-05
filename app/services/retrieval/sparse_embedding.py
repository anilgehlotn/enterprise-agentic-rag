"""BM25 sparse vectors via fastembed (Qdrant/bm25).

The model is a tokenizer + stemmer + stopword list, not a neural network: no PyTorch,
no GPU, a few MB downloaded once to FASTEMBED_CACHE_PATH (default: a temp dir).

fastembed produces term-frequency weights with BM25 length normalisation. The IDF
part of BM25 is applied by Qdrant at query time because the collection's "bm25"
sparse vector uses Modifier.IDF, so IDF always reflects the current corpus.

fastembed is imported lazily so importing this module needs no model download.
"""

from functools import lru_cache

import logfire
from qdrant_client import models

from app.services.retrieval.schema import BM25_MODEL

SPARSE_BATCH_SIZE = 64


@lru_cache(maxsize=1)
def get_sparse_model():
    from fastembed import SparseTextEmbedding

    logfire.info(f"Loading sparse model {BM25_MODEL} (fastembed).")
    return SparseTextEmbedding(model_name=BM25_MODEL)


def _to_qdrant(embedding) -> models.SparseVector:
    return models.SparseVector(indices=embedding.indices.tolist(), values=embedding.values.tolist())


def embed_sparse_documents(texts: list[str], batch_size: int = SPARSE_BATCH_SIZE) -> list[models.SparseVector]:
    """BM25 document vectors, embedded batch_size texts at a time."""
    model = get_sparse_model()
    vectors: list[models.SparseVector] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        with logfire.span("Sparse embed batch", model=BM25_MODEL, start=start, size=len(batch)):
            vectors.extend(_to_qdrant(e) for e in model.embed(batch, batch_size=batch_size))
    return vectors


def embed_sparse_query(query: str) -> models.SparseVector:
    """BM25 query vector (query_embed: every query term weighted 1, IDF added by Qdrant)."""
    return _to_qdrant(next(iter(get_sparse_model().query_embed(query))))
