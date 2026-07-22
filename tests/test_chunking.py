from app.ingestion.chunking.splitter import chunk_text


def test_chunker_preserves_all_paragraphs_under_limit():
    text = "First paragraph.\n\nSecond paragraph."

    assert chunk_text(text, chunk_size=100) == [text]


def test_chunker_splits_large_paragraphs():
    text = "x" * 25

    chunks = chunk_text(text, chunk_size=10)

    assert "".join(chunks) == text
    assert all(len(chunk) <= 10 for chunk in chunks)
