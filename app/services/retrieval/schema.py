"""Collection schema constants, retrieval modes and retrieval errors.

Pure module: no Qdrant, embedding, settings or network imports. The agent nodes and
the MCP server import it at module level, so it must stay cheap to import.
"""

import os

# ── Collection schema (one collection, two named vectors) ─────────────────────
DENSE_VECTOR = "dense"      # Gemini embedding, cosine distance
SPARSE_VECTOR = "bm25"      # fastembed Qdrant/bm25 term weights; Qdrant applies IDF (Modifier.IDF)
BM25_MODEL = "Qdrant/bm25"

REINGEST_COMMAND = "python -m app.ingestion.processor DATA --wipe"
OLD_SCHEMA_MESSAGE = f"Collection uses the old schema — re-ingest with: {REINGEST_COMMAND}"

# ── Retrieval modes ────────────────────────────────────────────────────────────
RETRIEVAL_MODE_ENV = "RETRIEVAL_MODE"
HYBRID = "hybrid"           # dense + BM25 prefetch, fused with Qdrant's native RRF
DENSE = "dense"             # dense vector only
RETRIEVAL_MODES = (HYBRID, DENSE)
DEFAULT_RETRIEVAL_MODE = HYBRID

# User-facing text when the vector database / embedding service can't be reached.
KNOWLEDGE_BASE_UNAVAILABLE_STATUS = "Knowledge base unavailable."
KNOWLEDGE_BASE_UNAVAILABLE_ANSWER = (
    "The knowledge base is temporarily unavailable, so I can't search the indexed "
    "documents right now. Please try again in a few minutes."
)


class RetrievalError(RuntimeError):
    """The knowledge base could not be searched (Qdrant or the embedding service failed).

    Raised instead of returning [] so an outage is never mistaken for "no results".
    """


class CollectionSchemaError(RetrievalError):
    """The Qdrant collection doesn't have the named "dense" + "bm25" vectors this code expects."""

    def __init__(self, detail: str = ""):
        message = OLD_SCHEMA_MESSAGE if not detail else f"{OLD_SCHEMA_MESSAGE} ({detail})"
        super().__init__(message)


def resolve_retrieval_mode(value: str | None = None) -> str:
    """
    Returns "hybrid" or "dense". Reads RETRIEVAL_MODE when value is None (default hybrid).
    Any other value raises ValueError, so a typo can't silently change retrieval.
    """
    if value is None:
        value = os.getenv(RETRIEVAL_MODE_ENV) or DEFAULT_RETRIEVAL_MODE
    mode = value.strip().lower()
    if mode not in RETRIEVAL_MODES:
        raise ValueError(
            f"Invalid {RETRIEVAL_MODE_ENV}={value!r}: expected one of {', '.join(RETRIEVAL_MODES)}."
        )
    return mode
