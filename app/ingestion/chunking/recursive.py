"""Recursive character chunker (the idea of LangChain's RecursiveCharacterTextSplitter).

1. Split the text on the first separator in SEPARATORS that occurs in it
   (paragraph break, then line break, then sentence end, then space, then characters).
2. Any piece still longer than chunk_size is split again with the next separator.
3. Consecutive pieces are merged greedily into chunks of at most chunk_size; when a
   chunk is full, the next one starts with the last <= chunk_overlap characters of the
   previous one, cut at a word start.

So boundaries fall on the coarsest unit that fits: a whole paragraph if possible,
else whole lines, else whole sentences, else words. Separators are kept at the end of
the piece they follow, so joining the pieces reproduces the text exactly. As in
LangChain, overlap is applied between chunks merged from the same parent piece.

One deliberate difference from LangChain: LangChain carries over WHOLE pieces, so when
the pieces are sentences longer than chunk_overlap (most prose: ~70-150 characters vs
a 75-character overlap at 500/15%) there is no overlap at all. Here the overlap is a
word-aligned tail, so the requested overlap is actually applied.

Implemented here (about 50 lines) rather than imported from langchain-text-splitters:
no new dependency for CI or the Docker image, and the guarantees the tests check
(never longer than chunk_size, nothing lost, word-aligned overlap) are ours to keep.
"""

SEPARATORS = ("\n\n", "\n", ". ", " ", "")


def _split_keep(text: str, separator: str) -> list[str]:
    """Split on separator, keeping it at the end of each piece ("" splits into characters)."""
    if not separator:
        return list(text)
    parts = text.split(separator)
    pieces = [part + separator for part in parts[:-1]] + [parts[-1]]
    return [piece for piece in pieces if piece]


def _word_tail(text: str, max_chars: int) -> str:
    """The last <= max_chars characters of text, starting at the beginning of a word ("" if none fits)."""
    if max_chars <= 0:
        return ""
    start = max(len(text) - max_chars, 0)
    # Move forward past whitespace and past the rest of a word cut in half.
    while start < len(text) and (text[start].isspace() or (start > 0 and not text[start - 1].isspace())):
        start += 1
    return text[start:]


def _merge(pieces: list[str], chunk_size: int, chunk_overlap: int) -> list[str]:
    """Greedily pack consecutive pieces (each <= chunk_size) into chunks with a word-aligned overlap."""
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current.strip() and len(current) + len(piece) > chunk_size:
            chunks.append(current)
            # The next chunk starts with the previous one's tail, as long as the new piece still fits.
            current = _word_tail(current, min(chunk_overlap, chunk_size - len(piece)))
        current += piece
    if current.strip():
        chunks.append(current)
    return chunks


def _split(text: str, chunk_size: int, chunk_overlap: int, separators: tuple[str, ...]) -> list[str]:
    index = next(i for i, sep in enumerate(separators) if sep == "" or sep in text)
    separator, finer = separators[index], separators[index + 1 :]

    chunks: list[str] = []
    fitting: list[str] = []
    for piece in _split_keep(text, separator):
        if len(piece) <= chunk_size:
            fitting.append(piece)
            continue
        if fitting:
            chunks.extend(_merge(fitting, chunk_size, chunk_overlap))
            fitting = []
        chunks.extend(_split(piece, chunk_size, chunk_overlap, finer))
    if fitting:
        chunks.extend(_merge(fitting, chunk_size, chunk_overlap))
    return chunks


def recursive_chunk(text: str, chunk_size: int, chunk_overlap: int = 0) -> list[str]:
    """Recursive separator-based chunks of at most chunk_size characters (see module docstring)."""
    if chunk_size <= 0 or not 0 <= chunk_overlap < chunk_size:
        raise ValueError("chunk_size must be positive and 0 <= chunk_overlap < chunk_size.")
    if not text.strip():
        return []
    chunks = (chunk.strip() for chunk in _split(text, chunk_size, chunk_overlap, SEPARATORS))
    return [chunk for chunk in chunks if chunk]
