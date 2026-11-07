"""Semantic chunker: split where the meaning shifts between neighbouring sentences.

1. Split the text into sentences (at ". ", "! ", "? " and line breaks). A sentence
   longer than chunk_size is cut into word-aligned fixed windows first.
2. Embed every sentence with the injected embed_fn and compute the cosine similarity
   between each pair of neighbouring sentences.
3. Breakpoint threshold = the breakpoint_percentile-th percentile of those similarities,
   computed per document (default 10: the 10% least similar neighbour pairs).
4. Walk the sentences and start a new chunk where the similarity drops below the
   threshold (once the chunk holds at least min_chunk_fraction * chunk_size
   characters), or where adding the next sentence would exceed chunk_size.

Why the minimum: with a percentile threshold alone, the chunk length is set by the
threshold, not by chunk_size. On this corpus every sentence-level dip produced chunks
of ~250-300 characters at any chunk_size (lines of DOCX/HTML text are short "sentences").
Requiring half of chunk_size before a semantic break makes chunk_size meaningful (mean
chunk ≈ 0.7 x chunk_size) and avoids heading-sized fragments.

Sentences: line breaks count as boundaries because the parsers emit one line per DOCX
paragraph, HTML block, slide shape or YAML line; for PDFs that also splits wrapped prose
lines, which the minimum chunk length then glues back together.

Chunks are slices of the original text, so their length is exact and nothing is lost.
No overlap: a semantic boundary is meant to be a clean topic break.

Cost: every sentence is embedded once at ingestion, on top of the chunk embeddings
(roughly 10-20x more embedding calls than chunks). The embedder is injectable so
tests use a fake one and the experiment can reuse a cached local model.
"""

import re
from collections.abc import Callable, Sequence

from app.ingestion.chunking.fixed import fixed_spans

EmbedFn = Callable[[list[str]], Sequence[Sequence[float]]]

DEFAULT_BREAKPOINT_PERCENTILE = 10.0
DEFAULT_MIN_CHUNK_FRACTION = 0.5

# A sentence ends after . ! or ? followed by whitespace, or at a line break.
_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\s*\n\s*")


def sentence_spans(text: str, max_len: int) -> list[tuple[int, int]]:
    """(start, end) offsets of the sentences in text; none longer than max_len, none empty."""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in [*_BOUNDARY.finditer(text), None]:
        end = match.start() if match else len(text)
        piece = text[start:end]
        if piece.strip():
            lead = len(piece) - len(piece.lstrip())
            s, e = start + lead, start + len(piece.rstrip())
            if e - s <= max_len:
                spans.append((s, e))
            else:
                spans.extend((s + a, s + b) for a, b in fixed_spans(text[s:e], max_len))
        if match:
            start = match.end()
    return spans


def _cosine_neighbours(vectors: Sequence[Sequence[float]]) -> list[float]:
    import numpy as np

    matrix = np.asarray(vectors, dtype=float)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    unit = matrix / np.where(norms == 0, 1.0, norms)
    return np.sum(unit[:-1] * unit[1:], axis=1).tolist()


def _percentile(values: list[float], percentile: float) -> float:
    import numpy as np

    return float(np.percentile(values, percentile))


class SemanticChunker:
    """Callable with the common chunker signature: chunker(text, chunk_size, chunk_overlap=0)."""

    def __init__(
        self,
        embed_fn: EmbedFn,
        breakpoint_percentile: float = DEFAULT_BREAKPOINT_PERCENTILE,
        min_chunk_fraction: float = DEFAULT_MIN_CHUNK_FRACTION,
    ):
        if not 0 <= breakpoint_percentile <= 100:
            raise ValueError("breakpoint_percentile must be between 0 and 100.")
        if not 0 <= min_chunk_fraction <= 1:
            raise ValueError("min_chunk_fraction must be between 0 and 1.")
        self.embed_fn = embed_fn
        self.breakpoint_percentile = breakpoint_percentile
        self.min_chunk_fraction = min_chunk_fraction

    def __call__(self, text: str, chunk_size: int, chunk_overlap: int = 0) -> list[str]:
        if chunk_overlap:
            raise ValueError("The semantic strategy doesn't support overlap.")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive.")
        spans = sentence_spans(text, chunk_size)
        if len(spans) <= 1:
            return [text[s:e] for s, e in spans]

        vectors = self.embed_fn([text[s:e] for s, e in spans])
        if len(vectors) != len(spans):
            raise RuntimeError(f"embed_fn returned {len(vectors)} vectors for {len(spans)} sentences.")
        similarities = _cosine_neighbours(vectors)
        threshold = _percentile(similarities, self.breakpoint_percentile)
        min_chars = int(chunk_size * self.min_chunk_fraction)

        chunks: list[str] = []
        chunk_start, chunk_end = spans[0]
        for (start, end), similarity in zip(spans[1:], similarities, strict=True):
            topic_break = similarity < threshold and chunk_end - chunk_start >= min_chars
            if topic_break or end - chunk_start > chunk_size:
                chunks.append(text[chunk_start:chunk_end])
                chunk_start = start
            chunk_end = end
        chunks.append(text[chunk_start:chunk_end])
        return chunks
