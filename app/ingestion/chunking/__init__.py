"""Chunking strategies behind one interface: chunker(text, chunk_size, chunk_overlap) -> list[str].

    paragraph  app/ingestion/chunking/splitter.py   (default; the original chunker)
    fixed      app/ingestion/chunking/fixed.py
    recursive  app/ingestion/chunking/recursive.py
    semantic   app/ingestion/chunking/semantic.py   (needs an embedding function)

Selected with CHUNKING_STRATEGY / CHUNK_SIZE / CHUNK_OVERLAP (see config.py).
Importing this package loads no embedding model.
"""

from collections.abc import Callable

import logfire

from app.ingestion.chunking.config import (
    FIXED,
    PARAGRAPH,
    RECURSIVE,
    SEMANTIC,
    STRATEGIES,
    ChunkingConfig,
    resolve_chunking_config,
)
from app.ingestion.chunking.fixed import fixed_chunk
from app.ingestion.chunking.recursive import recursive_chunk
from app.ingestion.chunking.semantic import EmbedFn, SemanticChunker
from app.ingestion.chunking.splitter import paragraph_chunk

Chunker = Callable[[str, int, int], list[str]]

__all__ = [
    "STRATEGIES",
    "Chunker",
    "ChunkingConfig",
    "chunk_text",
    "get_chunker",
    "resolve_chunking_config",
]


def _default_embed_fn(texts: list[str]):
    # The ingestion embedder (Gemini, with the sentence-transformers fallback), imported on first use.
    from app.services.retrieval.embedding import embed_texts

    return embed_texts(texts)


def get_chunker(strategy: str, embed_fn: EmbedFn | None = None) -> Chunker:
    """The chunker for strategy. embed_fn is used by "semantic" only (default: the ingestion embedder)."""
    if strategy == PARAGRAPH:
        return paragraph_chunk
    if strategy == FIXED:
        return fixed_chunk
    if strategy == RECURSIVE:
        return recursive_chunk
    if strategy == SEMANTIC:
        return SemanticChunker(embed_fn or _default_embed_fn)
    raise ValueError(f"Unknown chunking strategy {strategy!r}: expected one of {', '.join(STRATEGIES)}.")


def chunk_text(text: str, config: ChunkingConfig | None = None, embed_fn: EmbedFn | None = None) -> list[str]:
    """Chunks text with config (default: from the environment). Empty or blank text → []."""
    config = config or resolve_chunking_config()
    with logfire.span("✂️ Text Chunking", text_length=len(text), strategy=config.strategy):
        if not text.strip():
            return []
        chunks = get_chunker(config.strategy, embed_fn)(text, config.chunk_size, config.chunk_overlap)
        chunks = [chunk for chunk in chunks if chunk.strip()]
        logfire.info(f"✅ Generated {len(chunks)} chunks ({config.label})")
        return chunks
