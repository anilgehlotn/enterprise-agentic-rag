"""Paragraph chunker: the original strategy, kept unchanged as the default and the baseline."""


def _split_oversized_paragraph(paragraph: str, chunk_size: int) -> list[str]:
    """Split a single long paragraph without silently exceeding the chunk limit."""
    if len(paragraph) <= chunk_size:
        return [paragraph]

    chunks: list[str] = []
    remaining = paragraph
    while len(remaining) > chunk_size:
        split_at = remaining.rfind(" ", 0, chunk_size + 1)
        if split_at <= 0:
            split_at = chunk_size
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def paragraph_chunk(text: str, chunk_size: int = 1500, chunk_overlap: int = 0) -> list[str]:
    """
    Packs whole paragraphs ("\\n\\n"-separated) into chunks of at most chunk_size characters.
    A paragraph longer than chunk_size is split at the last space before the limit.
    No overlap: chunk_overlap must be 0 (validated by ChunkingConfig).
    """
    if chunk_overlap:
        raise ValueError("The paragraph strategy doesn't support overlap.")
    if not text.strip():
        return []

    chunks = []
    current_chunk = ""
    for paragraph in text.split("\n\n"):
        for p in _split_oversized_paragraph(paragraph, chunk_size):
            separator = "\n\n" if current_chunk else ""
            if len(current_chunk) + len(separator) + len(p) <= chunk_size:
                current_chunk += separator + p
            else:
                if current_chunk.strip():
                    chunks.append(current_chunk.strip())
                current_chunk = p

    if current_chunk.strip():
        chunks.append(current_chunk.strip())
    return [c for c in chunks if c.strip()]
