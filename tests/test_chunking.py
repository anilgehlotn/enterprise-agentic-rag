"""Chunking strategies: shared guarantees, strategy-specific boundaries, config validation."""

import re

import pytest

from app.ingestion.chunking import chunk_text, get_chunker
from app.ingestion.chunking.config import ChunkingConfig, resolve_chunking_config, validate_chunking
from app.ingestion.chunking.fixed import fixed_chunk
from app.ingestion.chunking.recursive import recursive_chunk
from app.ingestion.chunking.semantic import SemanticChunker, sentence_spans
from app.ingestion.chunking.splitter import paragraph_chunk


def fake_embed(texts):
    """Deterministic bag-of-letters vectors: similar wording → similar vectors. No model, no network."""
    vectors = []
    for text in texts:
        vector = [0.0] * 26
        for char in text.lower():
            if "a" <= char <= "z":
                vector[ord(char) - 97] += 1.0
        vectors.append(vector)
    return vectors


CHUNKERS = {
    "paragraph": paragraph_chunk,
    "fixed": fixed_chunk,
    "recursive": recursive_chunk,
    "semantic": SemanticChunker(fake_embed),
}
OVERLAP_CHUNKERS = {"fixed": fixed_chunk, "recursive": recursive_chunk}

SENTENCES = [
    "Kubernetes Jobs run Pods until a number of them complete successfully.",
    "The backoffLimit field sets how many retries a Job gets before it is marked failed.",
    "A CronJob creates Jobs on a repeating schedule written in cron syntax.",
    "Set concurrencyPolicy to Forbid so a slow run never overlaps the next one.",
    "The Horizontal Pod Autoscaler scales replicas from observed CPU utilisation.",
    "Install the Metrics Server first, otherwise the autoscaler has no data to act on.",
]
DOCUMENT = "\n\n".join(
    [
        " ".join(SENTENCES[:3]),
        "spec:\n  parallelism: 2\n  completions: 5\n  backoffLimit: 4",
        " ".join(SENTENCES[3:]),
        " ".join(SENTENCES),
    ]
)


def words(text):
    return re.findall(r"\S+", text)


# ── Guarantees shared by every strategy ──────────────────────────────────────


@pytest.mark.parametrize("name", CHUNKERS)
@pytest.mark.parametrize("size", [60, 150, 400])
def test_chunks_never_exceed_size_and_are_never_empty(name, size):
    chunks = CHUNKERS[name](DOCUMENT, size, 0)

    assert chunks
    assert all(0 < len(chunk) <= size for chunk in chunks)
    assert all(chunk == chunk.strip() for chunk in chunks)


@pytest.mark.parametrize("name", CHUNKERS)
@pytest.mark.parametrize("size", [60, 150, 400])
def test_no_word_is_lost(name, size):
    chunks = CHUNKERS[name](DOCUMENT, size, 0)
    chunk_words = {word for chunk in chunks for word in words(chunk)}

    assert set(words(DOCUMENT)) <= chunk_words


@pytest.mark.parametrize("name", CHUNKERS)
@pytest.mark.parametrize("text", ["", "   ", "\n\n\n"])
def test_empty_input_gives_no_chunks(name, text):
    assert CHUNKERS[name](text, 100, 0) == []


@pytest.mark.parametrize("name", CHUNKERS)
def test_one_very_long_word_is_split_into_pieces_within_the_limit(name):
    word = "x" * 25 + "y" * 25

    chunks = CHUNKERS[name](word, 10, 0)

    assert all(0 < len(chunk) <= 10 for chunk in chunks)
    assert "".join(chunks) == word


@pytest.mark.parametrize("name", OVERLAP_CHUNKERS)
def test_overlap_repeats_the_end_of_each_chunk_at_the_start_of_the_next(name):
    text = " ".join(SENTENCES)  # one paragraph, so recursive merges at sentence level

    chunks = OVERLAP_CHUNKERS[name](text, 120, 40)

    assert len(chunks) > 2
    assert all(len(chunk) <= 120 for chunk in chunks)
    shared = [_longest_suffix_prefix(a, b) for a, b in zip(chunks, chunks[1:], strict=False)]
    assert all(0 < length <= 40 for length in shared), shared
    assert set(words(text)) <= {w for c in chunks for w in words(c)}


def test_recursive_overlap_works_when_sentences_are_longer_than_the_overlap():
    # LangChain carries whole pieces, so 70-80 char sentences with a 40-char overlap get none.
    chunks = recursive_chunk(" ".join(SENTENCES), 160, 40)

    assert all(0 < _longest_suffix_prefix(a, b) <= 40 for a, b in zip(chunks, chunks[1:], strict=False))


@pytest.mark.parametrize("name", OVERLAP_CHUNKERS)
def test_no_overlap_means_consecutive_chunks_share_nothing(name):
    text = " ".join(SENTENCES)

    chunks = OVERLAP_CHUNKERS[name](text, 120, 0)

    assert all(_longest_suffix_prefix(a, b) == 0 for a, b in zip(chunks, chunks[1:], strict=False))
    assert sum(len(words(c)) for c in chunks) == len(words(text))


def _longest_suffix_prefix(a: str, b: str) -> int:
    """Length of the longest suffix of a (at a word start) that b starts with."""
    for length in range(min(len(a), len(b)), 0, -1):
        if b.startswith(a[-length:]) and (length == len(a) or a[-length - 1].isspace()):
            return length
    return 0


@pytest.mark.parametrize("name", ["paragraph", "semantic"])
def test_paragraph_and_semantic_reject_overlap(name):
    with pytest.raises(ValueError, match="overlap"):
        CHUNKERS[name](DOCUMENT, 100, 10)


# ── Fixed ─────────────────────────────────────────────────────────────────────


def test_fixed_snaps_to_word_boundaries():
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa"

    chunks = fixed_chunk(text, 16, 0)

    assert chunks == ["alpha beta gamma", "delta epsilon", "zeta eta theta", "iota kappa"]


def test_fixed_overlap_starts_on_a_word():
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa"

    chunks = fixed_chunk(text, 22, 8)

    assert chunks[0] == "alpha beta gamma delta"
    assert chunks[1].startswith("delta ")  # backed off 8 chars ("a delta"), then forward to a word start
    assert all(set(words(c)) <= set(words(text)) for c in chunks)


def test_fixed_rejects_overlap_not_smaller_than_size():
    with pytest.raises(ValueError):
        fixed_chunk("some text", 10, 10)


# ── Recursive ─────────────────────────────────────────────────────────────────


def test_recursive_prefers_paragraph_boundaries():
    text = "First paragraph is here.\n\nSecond paragraph is here.\n\nThird paragraph is here."

    assert recursive_chunk(text, 30, 0) == [
        "First paragraph is here.",
        "Second paragraph is here.",
        "Third paragraph is here.",
    ]


def test_recursive_falls_back_to_line_then_sentence_boundaries():
    lines = "line one is short\nline two is short\nline three is short"
    assert recursive_chunk(lines, 40, 0) == ["line one is short\nline two is short", "line three is short"]

    sentences = "Sentence one is here. Sentence two is here. Sentence three is here."
    assert recursive_chunk(sentences, 45, 0) == [
        "Sentence one is here. Sentence two is here.",
        "Sentence three is here.",
    ]


def test_recursive_keeps_a_fitting_paragraph_whole_instead_of_splitting_mid_sentence():
    text = "Short intro.\n\n" + "A longer paragraph that fits. It has two sentences."

    chunks = recursive_chunk(text, 60, 0)

    assert chunks[-1] == "A longer paragraph that fits. It has two sentences."


# ── Paragraph (legacy behaviour kept) ────────────────────────────────────────


def test_paragraph_preserves_all_paragraphs_under_limit():
    text = "First paragraph.\n\nSecond paragraph."

    assert paragraph_chunk(text, chunk_size=100) == [text]


def test_paragraph_splits_large_paragraphs():
    text = "x" * 25

    chunks = paragraph_chunk(text, chunk_size=10)

    assert "".join(chunks) == text
    assert all(len(chunk) <= 10 for chunk in chunks)


# ── Semantic ──────────────────────────────────────────────────────────────────


def topic_embed(texts):
    """Sentences about cats point one way, sentences about cars the other."""
    return [[1.0, 0.0] if "cat" in t else [0.0, 1.0] for t in texts]


CAT_CAR = (
    "The cat sleeps all day. A cat likes warm places. My cat purrs loudly. "
    "The car needs new tyres. A car engine is loud. That car is red."
)


def test_semantic_splits_where_neighbour_similarity_drops():
    chunks = SemanticChunker(topic_embed, breakpoint_percentile=10, min_chunk_fraction=0)(CAT_CAR, 500, 0)

    assert chunks == [
        "The cat sleeps all day. A cat likes warm places. My cat purrs loudly.",
        "The car needs new tyres. A car engine is loud. That car is red.",
    ]


def test_semantic_respects_max_chunk_size_inside_one_topic():
    chunks = SemanticChunker(topic_embed, min_chunk_fraction=0)(CAT_CAR, 50, 0)

    assert all(len(chunk) <= 50 for chunk in chunks)
    assert not any("cat" in chunk and "car " in chunk for chunk in chunks)


def test_semantic_min_chunk_fraction_suppresses_tiny_topic_breaks():
    text = "A cat. The car needs new tyres. A car engine is loud. That car is red."

    with_min = SemanticChunker(topic_embed, min_chunk_fraction=0.5)(text, 100, 0)
    without_min = SemanticChunker(topic_embed, min_chunk_fraction=0)(text, 100, 0)

    assert without_min[0] == "A cat."
    assert with_min == [text]


def test_semantic_embeds_each_sentence_once_per_call():
    calls = []

    def recording_embed(texts):
        calls.append(list(texts))
        return topic_embed(texts)

    SemanticChunker(recording_embed)(CAT_CAR, 500, 0)

    assert len(calls) == 1 and len(calls[0]) == 6


def test_semantic_rejects_wrong_number_of_vectors():
    with pytest.raises(RuntimeError, match="vectors"):
        SemanticChunker(lambda texts: [[1.0]])(CAT_CAR, 500, 0)


def test_sentence_spans_split_on_sentence_ends_and_line_breaks():
    text = "One. Two!  Three?\nfour five\n\n  six"

    assert [text[s:e] for s, e in sentence_spans(text, 100)] == ["One.", "Two!", "Three?", "four five", "six"]


def test_semantic_parameters_are_validated():
    with pytest.raises(ValueError):
        SemanticChunker(fake_embed, breakpoint_percentile=101)
    with pytest.raises(ValueError):
        SemanticChunker(fake_embed, min_chunk_fraction=2)


# ── Config + dispatch ────────────────────────────────────────────────────────


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("CHUNKING_STRATEGY", "CHUNK_SIZE", "CHUNK_OVERLAP"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_defaults_keep_the_original_chunker(clean_env):
    config = resolve_chunking_config()

    assert config == ChunkingConfig("paragraph", 1500, 0)
    assert config.as_metadata() == {"chunking_strategy": "paragraph", "chunk_size": 1500, "chunk_overlap": 0}


def test_config_is_read_from_the_environment(clean_env):
    clean_env.setenv("CHUNKING_STRATEGY", " Recursive ")
    clean_env.setenv("CHUNK_SIZE", "800")
    clean_env.setenv("CHUNK_OVERLAP", "120")

    assert resolve_chunking_config() == ChunkingConfig("recursive", 800, 120)


def test_empty_env_values_fall_back_to_defaults(clean_env):
    clean_env.setenv("CHUNKING_STRATEGY", "")
    clean_env.setenv("CHUNK_SIZE", " ")

    assert resolve_chunking_config() == ChunkingConfig()


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"CHUNKING_STRATEGY": "sentences"}, "Invalid CHUNKING_STRATEGY='sentences': expected one of"),
        ({"CHUNK_SIZE": "big"}, "Invalid CHUNK_SIZE='big'"),
        ({"CHUNK_SIZE": "0"}, "Invalid CHUNK_SIZE=0"),
        ({"CHUNK_OVERLAP": "-5"}, "Invalid CHUNK_OVERLAP=-5"),
        ({"CHUNKING_STRATEGY": "fixed", "CHUNK_SIZE": "500", "CHUNK_OVERLAP": "500"}, "must be smaller than"),
        ({"CHUNK_OVERLAP": "100"}, "'paragraph' strategy doesn't support overlap"),
        ({"CHUNKING_STRATEGY": "semantic", "CHUNK_OVERLAP": "100"}, "'semantic' strategy doesn't support overlap"),
    ],
)
def test_invalid_config_raises_a_clear_error(clean_env, env, message):
    for name, value in env.items():
        clean_env.setenv(name, value)

    with pytest.raises(ValueError, match=re.escape(message)):
        resolve_chunking_config()


def test_validate_rejects_non_integer_sizes():
    with pytest.raises(ValueError, match="CHUNK_SIZE"):
        validate_chunking("fixed", 10.5, 0)
    with pytest.raises(ValueError, match="CHUNK_OVERLAP"):
        validate_chunking("fixed", 10, True)


def test_chunk_text_dispatches_on_the_config():
    config = ChunkingConfig("fixed", 16, 0)

    assert chunk_text("alpha beta gamma delta epsilon", config) == ["alpha beta gamma", "delta epsilon"]
    assert chunk_text("   ", config) == []


def test_chunk_text_uses_the_environment_by_default(clean_env):
    clean_env.setenv("CHUNKING_STRATEGY", "fixed")
    clean_env.setenv("CHUNK_SIZE", "16")

    assert chunk_text("alpha beta gamma delta epsilon") == ["alpha beta gamma", "delta epsilon"]


def test_semantic_chunk_text_uses_the_injected_embedder():
    calls = []

    def recording_embed(texts):
        calls.append(len(texts))
        return topic_embed(texts)

    chunks = chunk_text(CAT_CAR, ChunkingConfig("semantic", 130, 0), embed_fn=recording_embed)

    assert calls == [6]
    assert chunks == SemanticChunker(topic_embed)(CAT_CAR, 130, 0)


def test_get_chunker_rejects_unknown_strategy():
    with pytest.raises(ValueError, match="Unknown chunking strategy"):
        get_chunker("sentences")


def test_semantic_default_embedder_is_imported_lazily(monkeypatch):
    import sys
    from types import SimpleNamespace

    calls = []

    def embed_texts(texts):
        calls.append(len(texts))
        return topic_embed(texts)

    monkeypatch.setitem(sys.modules, "app.services.retrieval.embedding", SimpleNamespace(embed_texts=embed_texts))

    get_chunker("semantic")(CAT_CAR, 500, 0)

    assert calls == [6]
