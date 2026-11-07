"""Fixed-size chunker: character windows of at most chunk_size, snapped to word boundaries.

Each window ends at the last whitespace before the size limit (hard cut only inside a
word longer than chunk_size). The next window starts chunk_overlap characters before
the previous end, moved forward to the start of a word, so the overlap never cuts a
word in half and is at most chunk_overlap characters.
"""


def _is_space(text: str, index: int) -> bool:
    return text[index].isspace()


def fixed_spans(text: str, chunk_size: int, chunk_overlap: int = 0) -> list[tuple[int, int]]:
    """(start, end) offsets into text of each chunk, stripped of surrounding whitespace."""
    if chunk_size <= 0 or not 0 <= chunk_overlap < chunk_size:
        raise ValueError("chunk_size must be positive and 0 <= chunk_overlap < chunk_size.")
    n = len(text)
    spans: list[tuple[int, int]] = []
    start = 0
    while start < n:
        while start < n and _is_space(text, start):
            start += 1
        if start >= n:
            break

        end = min(start + chunk_size, n)
        if end < n and not _is_space(text, end):
            # Snap back to the last whitespace inside the window; hard-cut a word longer than the window.
            cut = end
            while cut > start and not _is_space(text, cut - 1):
                cut -= 1
            if cut > start:
                end = cut
        stripped_end = end
        while stripped_end > start and _is_space(text, stripped_end - 1):
            stripped_end -= 1
        spans.append((start, stripped_end))
        if end >= n:
            break

        # Next window: back off by the overlap, then forward to a word start; always make progress.
        next_start = max(end - chunk_overlap, start + 1)
        while next_start < end and not _is_space(text, next_start - 1):
            next_start += 1
        start = next_start
    return spans


def fixed_chunk(text: str, chunk_size: int, chunk_overlap: int = 0) -> list[str]:
    """Fixed-size character windows with word-aligned overlap. Never longer than chunk_size."""
    return [text[start:end] for start, end in fixed_spans(text, chunk_size, chunk_overlap) if end > start]
