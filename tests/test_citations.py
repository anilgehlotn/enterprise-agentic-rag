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
    assert citations[0]["preview"].startswith("CronJobs")
