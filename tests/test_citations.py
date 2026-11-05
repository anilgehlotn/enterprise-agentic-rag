from app.schemas import SourceChunk
from app.services.retrieval.citations import build_citations, format_context


def test_format_context_includes_source_labels():
    context = format_context([
        {"source": "kubernetes.html", "content": "A Job creates one or more Pods."}
    ])

    assert "[Source: kubernetes.html]" in context
    assert "A Job creates one or more Pods." in context


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
