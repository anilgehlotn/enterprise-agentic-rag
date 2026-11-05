from app.schemas import QueryResponse, SourceChunk
from app.services.retrieval.citations import build_citations, format_context


def test_format_context_includes_source_labels():
    context = format_context([
        {"source": "kubernetes.html", "content": "A Job creates one or more Pods."}
    ])

    assert "[1] Source: kubernetes.html" in context
    assert "A Job creates one or more Pods." in context


def test_format_context_numbers_sources_in_order():
    context = format_context([
        {"source": "jobs.html", "content": "Jobs run to completion."},
        {"source": "cron.docx", "content": "CronJobs run on a schedule."},
    ])

    assert context == (
        "[1] Source: jobs.html\nJobs run to completion.\n\n"
        "[2] Source: cron.docx\nCronJobs run on a schedule."
    )


def test_context_numbers_match_citation_positions_when_chunks_are_empty():
    """[n] cited in the answer must be sources[n-1] in the API response."""
    records = [
        {"source": "a.txt", "content": "Alpha."},
        {"source": "empty.txt", "content": "   "},
        {"source": "b.txt", "content": "Beta."},
    ]

    context = format_context(records)
    citations = build_citations(records)

    assert "[2] Source: b.txt" in context
    assert "empty.txt" not in context
    assert citations[1]["source"] == "b.txt"


def test_build_citations_preserves_retrieval_metadata():
    citations = build_citations([
        {
            "source": "cronjobs.docx",
            "source_type": "true",
            "content": "CronJobs create Jobs on a repeating schedule.",
            "score": 0.81,
            "rerank_score": 0.92,
        }
    ])

    assert citations[0]["source"] == "cronjobs.docx"
    assert citations[0]["rerank_score"] == 0.92
    assert citations[0]["retrieval_score"] == 0.81
    assert citations[0]["preview"].startswith("CronJobs")


def test_source_chunk_fields_match_build_citations_keys():
    """Every key build_citations emits must be a SourceChunk field, or FastAPI drops it."""
    citation = build_citations([{"source": "a.txt", "content": "text", "score": 0.5}])[0]

    assert set(citation) == set(SourceChunk.model_fields)


def test_source_chunk_keeps_vector_score_and_preview():
    citation = build_citations(
        [{"source": "jobs.html", "content": "x" * 400, "score": 0.73, "rerank_score": 0.9}],
        preview_chars=50,
    )[0]

    # This is what FastAPI's response_model does to each element of `sources`.
    chunk = SourceChunk.model_validate(citation).model_dump()

    assert chunk["retrieval_score"] == 0.73
    assert chunk["rerank_score"] == 0.9
    assert chunk["preview"] == "x" * 50
    assert chunk["content"] == "x" * 400


def test_source_chunk_allows_missing_scores():
    citation = build_citations([{"source": "notes.txt", "content": "hello"}])[0]

    chunk = SourceChunk.model_validate(citation)

    assert chunk.retrieval_score is None
    assert chunk.rerank_score is None


def test_query_response_exposes_grounded_and_defaults_to_none():
    base = {"question": "q", "answer": "a", "thought_process": [], "status": "ok", "sources": []}

    assert QueryResponse(**base).grounded is None
    assert QueryResponse(**base, grounded=True).grounded is True
    assert QueryResponse(**base, grounded=False).model_dump()["grounded"] is False
