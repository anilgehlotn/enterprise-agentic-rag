"""Chunking configuration: CHUNKING_STRATEGY, CHUNK_SIZE, CHUNK_OVERLAP.

Pure module (no logfire, no embedding, no network), so app.main can validate the
configuration at startup and tests can import it cheaply.

Defaults reproduce the chunker used before strategies were configurable:
paragraph, 1,500 characters, no overlap. evals/chunking_experiment.py compares the
strategies; change the defaults only after it has picked a winner.
"""

import os
from dataclasses import dataclass

CHUNKING_STRATEGY_ENV = "CHUNKING_STRATEGY"
CHUNK_SIZE_ENV = "CHUNK_SIZE"
CHUNK_OVERLAP_ENV = "CHUNK_OVERLAP"

PARAGRAPH = "paragraph"  # pack whole paragraphs (the original chunker)
FIXED = "fixed"  # fixed-size character windows snapped to word boundaries
RECURSIVE = "recursive"  # split by paragraph → line → sentence → word → character, then merge
SEMANTIC = "semantic"  # sentence embeddings; new chunk where neighbour similarity drops
STRATEGIES = (PARAGRAPH, FIXED, RECURSIVE, SEMANTIC)

# Strategies that cut at natural units (paragraphs, topic shifts) and don't repeat text.
NO_OVERLAP_STRATEGIES = (PARAGRAPH, SEMANTIC)

DEFAULT_STRATEGY = PARAGRAPH
DEFAULT_CHUNK_SIZE = 1500
DEFAULT_CHUNK_OVERLAP = 0


@dataclass(frozen=True)
class ChunkingConfig:
    strategy: str = DEFAULT_STRATEGY
    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP

    def __post_init__(self):
        validate_chunking(self.strategy, self.chunk_size, self.chunk_overlap)

    def as_metadata(self) -> dict:
        """Fields stored with every chunk (Qdrant payload, processed_data JSON)."""
        return {
            "chunking_strategy": self.strategy,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
        }

    @property
    def label(self) -> str:
        return f"{self.strategy}/{self.chunk_size}/{self.chunk_overlap}"


def validate_chunking(strategy: str, chunk_size: int, chunk_overlap: int) -> None:
    """Raises ValueError with the offending setting named, so a typo can't silently change ingestion."""
    if strategy not in STRATEGIES:
        raise ValueError(f"Invalid {CHUNKING_STRATEGY_ENV}={strategy!r}: expected one of {', '.join(STRATEGIES)}.")
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError(f"Invalid {CHUNK_SIZE_ENV}={chunk_size!r}: expected a positive number of characters.")
    if isinstance(chunk_overlap, bool) or not isinstance(chunk_overlap, int) or chunk_overlap < 0:
        raise ValueError(f"Invalid {CHUNK_OVERLAP_ENV}={chunk_overlap!r}: expected 0 or a positive number.")
    if chunk_overlap >= chunk_size:
        raise ValueError(
            f"Invalid {CHUNK_OVERLAP_ENV}={chunk_overlap}: must be smaller than {CHUNK_SIZE_ENV}={chunk_size}."
        )
    if chunk_overlap and strategy in NO_OVERLAP_STRATEGIES:
        raise ValueError(
            f"Invalid {CHUNK_OVERLAP_ENV}={chunk_overlap}: the {strategy!r} strategy doesn't support overlap "
            f"(set {CHUNK_OVERLAP_ENV}=0, or use fixed or recursive)."
        )


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(f"Invalid {name}={raw!r}: expected an integer number of characters.") from None


def resolve_chunking_config() -> ChunkingConfig:
    """ChunkingConfig from the environment (empty/unset → defaults). Invalid values raise ValueError."""
    strategy = (os.getenv(CHUNKING_STRATEGY_ENV) or DEFAULT_STRATEGY).strip().lower()
    return ChunkingConfig(
        strategy=strategy,
        chunk_size=_int_env(CHUNK_SIZE_ENV, DEFAULT_CHUNK_SIZE),
        chunk_overlap=_int_env(CHUNK_OVERLAP_ENV, DEFAULT_CHUNK_OVERLAP),
    )
