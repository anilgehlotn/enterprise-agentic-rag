"""API response models that are shared with pure helpers (and unit-testable without the app)."""

from typing import Optional

from pydantic import BaseModel


class SourceChunk(BaseModel):
    """One piece of retrieved evidence. Field names must match build_citations()."""

    source: str
    source_type: str = "unknown"
    content: str
    preview: str = ""
    retrieval_score: Optional[float] = None
    rerank_score: Optional[float] = None
