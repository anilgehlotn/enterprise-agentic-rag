"""Utilities for keeping retrieval evidence attached to generated answers."""

from typing import Any


def format_context(records: list[dict[str, Any]], max_chars: int = 25_000) -> str:
    """Build an attributed prompt context without exceeding the model budget."""
    sections: list[str] = []
    current_length = 0

    for record in records:
        source = record.get("source", "Unknown source")
        content = record.get("content", "").strip()
        if not content:
            continue

        section = f"[Source: {source}]\n{content}"
        if current_length + len(section) > max_chars:
            break
        sections.append(section)
        current_length += len(section) + 2

    return "\n\n".join(sections)


def build_citations(records: list[dict[str, Any]], preview_chars: int = 280) -> list[dict[str, Any]]:
    """Return a UI/API-safe evidence list while preserving retrieval provenance."""
    citations = []
    for record in records:
        content = record.get("content", "").strip()
        if not content:
            continue
        citations.append(
            {
                "source": record.get("source", "Unknown source"),
                "source_type": record.get("source_type", "unknown"),
                "content": content,
                "preview": content[:preview_chars],
                "retrieval_score": record.get("score"),
                "rerank_score": record.get("rerank_score"),
            }
        )
    return citations
