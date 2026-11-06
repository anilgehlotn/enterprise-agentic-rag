"""API response models that are shared with pure helpers (and unit-testable without the app)."""

from pydantic import BaseModel

# Shared input limit for the /query API and the MCP ask_knowledge_base tool.
MAX_QUESTION_CHARS = 4_000


class SourceChunk(BaseModel):
    """One piece of retrieved evidence. Field names must match build_citations()."""

    source: str
    source_type: str = "unknown"
    content: str
    preview: str = ""
    retrieval_score: float | None = None
    rerank_score: float | None = None


class QueryResponse(BaseModel):
    question: str
    answer: str
    thought_process: list[str]
    status: str
    sources: list[SourceChunk]
    # Self-RAG verifier verdict: True = supported by sources, False = not fully
    # supported, None = not verified (conversational, no documents, blocked, unclear).
    grounded: bool | None = None
