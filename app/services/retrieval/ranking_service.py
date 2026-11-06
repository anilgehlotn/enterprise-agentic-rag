import os
import time

import logfire
from flashrank import Ranker, RerankRequest

# Lazy initialization - Ranker is loaded on first use to ensure logfire.configure() has run
_ranker = None

# Where FlashRank keeps its ONNX model. The Docker image pre-downloads the model into
# FLASHRANK_CACHE_DIR at build time so a cold start never downloads it.
FLASHRANK_CACHE_DIR_ENV = "FLASHRANK_CACHE_DIR"
DEFAULT_FLASHRANK_CACHE_DIR = "/tmp/flashrank"


def flashrank_cache_dir() -> str:
    """FlashRank model cache dir: $FLASHRANK_CACHE_DIR, or /tmp/flashrank when unset/empty."""
    return os.getenv(FLASHRANK_CACHE_DIR_ENV) or DEFAULT_FLASHRANK_CACHE_DIR


def _get_ranker() -> Ranker:
    """
    Initializes the FlashRank engine lazily.
    FlashRank uses a local ONNX cross-encoder (its default, ms-marco-TinyBERT-L-2-v2) for reranking.
    """
    global _ranker
    if _ranker is None:
        logfire.info("🧠 Initializing FlashRank Model (TinyBERT) locally...")
        try:
            # A fixed cache directory avoids permission issues in production and lets the
            # image ship the model pre-downloaded (see Dockerfile).
            _ranker = Ranker(cache_dir=flashrank_cache_dir())
        except Exception:
            _ranker = Ranker()
    return _ranker


def rerank_documents(query: str, documents: list[str], top_n: int = 5) -> list[str]:
    """
    Refines retrieval results by re-scoring documents against the query semantically.

    Why FlashRank?
    Standard vector search (Cosine Similarity) is fast but mathematically "fuzzy."
    FlashRank uses a Cross-Encoder approach which is much more precise but usually slow.
    FlashRank solves this by using highly optimized, quantized ONNX models locally.
    """
    if not documents:
        return []

    start_time = time.time()
    logfire.info(f"📡 [Reranker] Sending {len(documents)} docs to FlashRank Cross-Encoder...")

    try:
        ranker = _get_ranker()

        # FlashRank expects a list of dictionaries with 'id' and 'text'
        passages = [{"id": i, "text": doc} for i, doc in enumerate(documents)]

        request = RerankRequest(query=query, passages=passages)
        results = ranker.rerank(request)

        # Results are returned sorted by highest semantic score first
        reranked_docs = []
        for res in results[:top_n]:
            reranked_docs.append(res["text"])

        duration = time.time() - start_time
        top_score = results[0]["score"] if results else "N/A"
        logfire.info(f"✅ [Reranker] Done in {duration:.2f}s. Top semantic score: {top_score}")

        return reranked_docs

    except Exception as e:
        logfire.error(f"❌ [Reranker] Semantic Reranking Failed: {e}")
        # Fallback to the original Qdrant order to ensure the user still gets an answer
        return documents[:top_n]


def rerank_records(query: str, records: list[dict], top_n: int = 5) -> list[dict]:
    """Rerank retrieval records while retaining source and score metadata."""
    if not records:
        return []

    try:
        ranker = _get_ranker()
        passages = [
            {"id": index, "text": record.get("content", "")}
            for index, record in enumerate(records)
            if record.get("content")
        ]
        results = ranker.rerank(RerankRequest(query=query, passages=passages))

        ranked_records = []
        for result in results[:top_n]:
            record = dict(records[result["id"]])
            record["rerank_score"] = result.get("score")
            ranked_records.append(record)
        return ranked_records
    except Exception as exc:
        logfire.error(f"❌ [Reranker] Metadata-aware reranking failed: {exc}")
        return records[:top_n]
