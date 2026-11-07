r"""Pure retrieval metrics for the retrieval benchmark (no I/O, no network).

Match rule (chunk_matches)
--------------------------
A retrieved chunk "matches" a golden relevant_context when, after normalisation
(Unicode casefold + every run of whitespace collapsed to one space), EITHER:

  1. containment: one text contains the other. This covers a golden context that
     is a slice of a larger chunk, and a chunk that is a slice of a golden context
     that spans a chunk boundary; or
  2. token overlap ≥ 0.6: at least 60% of the golden context's distinct word
     BIGRAMS (pairs of consecutive \w+ tokens) also occur in the chunk. This tolerates
     the small differences between the golden text and what the parsers extract
     (bullets, punctuation, line breaks, a sentence cut by the chunker).

Why bigrams and not single words: on this corpus, single-word overlap matched
unrelated YAML manifests (spec / template / containers / restartPolicy appear in
every Job manifest: 0.64-0.71 overlap for a wrong CronJob chunk). With bigrams the
true matches score 0.63-1.0 and the best wrong chunk scores 0.34, so 0.6 separates
them cleanly. The overlap is measured against the GOLDEN context (recall of its
bigrams), not the chunk, because chunks (≤ 1,500 chars) are usually longer than
golden contexts. A golden context with fewer than MIN_OVERLAP_BIGRAMS bigrams can
only match by containment.

That rule assumes chunks of about 1,500 characters. To compare chunk SIZES fairly,
evals/chunking_experiment.py uses chunk_covers() instead, which measures the overlap
against the smaller of the two texts (rule and corpus check in its docstring).

Metrics at k (each question's ranked chunk list is cut to the first k)
------------------------------------------------------------------------
  hit rate@k    share of questions with at least one matching chunk in the top k
  MRR@k         mean over questions of 1 / rank of the first matching chunk
                (0 when no chunk in the top k matches)
  noise rate@k  share of all top-k chunks (pooled over questions) whose
                source_type is "noisy", i.e. from the distractor corpus; lower is better
  coverage@k    share of the golden contexts' bigrams present in the top-k chunks
                (context_coverage); how much of the evidence reaches the LLM
"""

import re
from collections.abc import Callable, Iterable, Sequence

OVERLAP_THRESHOLD = 0.6
MIN_OVERLAP_BIGRAMS = 3
# Partial-match rule (chunk_covers), used to compare chunk sizes fairly.
PARTIAL_OVERLAP_THRESHOLD = 0.5
MIN_SHARED_BIGRAMS = 8
NOISY_SOURCE_TYPE = "noisy"

_TOKEN = re.compile(r"\w+")

MatchFn = Callable[[str | None, str | None], bool]


def normalize(text: str | None) -> str:
    """Casefold and collapse all whitespace (spaces, tabs, newlines, indentation) to single spaces."""
    return " ".join((text or "").casefold().split())


def word_bigrams(text: str | None) -> set[tuple[str, str]]:
    """Distinct pairs of consecutive word tokens of the normalised text."""
    words = _TOKEN.findall(normalize(text))
    return set(zip(words, words[1:], strict=False))


def token_overlap(chunk: str | None, golden: str | None) -> float:
    """Share of the golden context's word bigrams that also appear in the chunk (0.0-1.0)."""
    golden_bigrams = word_bigrams(golden)
    if not golden_bigrams:
        return 0.0
    return len(golden_bigrams & word_bigrams(chunk)) / len(golden_bigrams)


def chunk_matches(chunk: str | None, golden: str | None, threshold: float = OVERLAP_THRESHOLD) -> bool:
    """True if the chunk matches the golden context (see the module docstring for the rule)."""
    chunk_text, golden_text = normalize(chunk), normalize(golden)
    if not chunk_text or not golden_text:
        return False
    if golden_text in chunk_text or chunk_text in golden_text:
        return True
    if len(word_bigrams(golden_text)) < MIN_OVERLAP_BIGRAMS:
        return False
    return token_overlap(chunk_text, golden_text) >= threshold


def partial_overlap(chunk: str | None, golden: str | None) -> float:
    """Shared word bigrams / bigrams of the SMALLER of the two texts (0.0-1.0). See chunk_covers()."""
    chunk_bigrams, golden_bigrams = word_bigrams(chunk), word_bigrams(golden)
    if not chunk_bigrams or not golden_bigrams:
        return 0.0
    return len(chunk_bigrams & golden_bigrams) / min(len(chunk_bigrams), len(golden_bigrams))


def chunk_covers(chunk: str | None, golden: str | None, threshold: float = PARTIAL_OVERLAP_THRESHOLD) -> bool:
    """
    Partial-match hit rule, fair across chunk sizes (used by evals/chunking_experiment.py).

    True when, after normalisation, one text contains the other, OR the two share at
    least MIN_SHARED_BIGRAMS word bigrams AND the shared bigrams are at least `threshold`
    (0.5) of the bigrams of the SMALLER text:

      * chunk larger than the golden context: ≥ 50% of the golden context is inside the chunk;
      * chunk smaller than the golden context: ≥ 50% of the chunk is golden text, i.e. the
        chunk is a real piece of the golden context, not a chunk that only grazes it.

    chunk_matches() measures recall of the GOLDEN bigrams only, so a 500-character chunk
    holding half of a 450-character golden context fails it even though it is the right
    passage; this rule counts it. The absolute floor stops a tiny chunk (a heading, a
    YAML line) from matching on a couple of shared bigrams.

    Checked on the real corpus (DATA/true_data + DATA/noisy_data parsed by the ingestion
    loaders, 19 golden contexts × the 114,160 distinct chunks of all 18 configurations of
    evals/chunking_experiment.py):
      * no chunk from another document passes; the highest score from another document
        is 0.31 (a pods_autoscale.html chunk vs a monitor_job golden context), well under 0.5;
      * all 395 passing chunk/golden pairs from the right document lie on the golden
        passage itself (located by character offset in the parsed document; one golden
        context of question 2 is stitched from two places and both count); the
        borderline passes (0.50-0.53) hold the first or second half of a golden context
        cut by a chunk boundary, and the near misses (0.3-0.49) hold a smaller sliver;
      * every golden context can be matched in every configuration, so no strategy or
        size is penalised by the rule itself;
      * without the MIN_SHARED_BIGRAMS floor exactly one wrong pair passes: the 4-bigram
        PDF line "This will take a while." against a minikube golden context.
    """
    chunk_text, golden_text = normalize(chunk), normalize(golden)
    if not chunk_text or not golden_text:
        return False
    if golden_text in chunk_text or chunk_text in golden_text:
        return True
    chunk_bigrams, golden_bigrams = word_bigrams(chunk_text), word_bigrams(golden_text)
    shared = len(chunk_bigrams & golden_bigrams)
    if shared < MIN_SHARED_BIGRAMS:
        return False
    return shared / min(len(chunk_bigrams), len(golden_bigrams)) >= threshold


def is_relevant(chunk: str | None, golden_contexts: Iterable[str], match: MatchFn = chunk_matches) -> bool:
    """True if the chunk matches any golden relevant_context (match: chunk_matches or chunk_covers)."""
    return any(match(chunk, golden) for golden in golden_contexts)


def first_relevant_rank(
    chunks: Sequence[str], golden_contexts: Sequence[str], k: int = 5, match: MatchFn = chunk_matches
) -> int | None:
    """1-based rank of the first matching chunk within the top k, or None."""
    for rank, chunk in enumerate(chunks[:k], start=1):
        if is_relevant(chunk, golden_contexts, match):
            return rank
    return None


def reciprocal_rank(
    chunks: Sequence[str], golden_contexts: Sequence[str], k: int = 5, match: MatchFn = chunk_matches
) -> float:
    rank = first_relevant_rank(chunks, golden_contexts, k, match)
    return 0.0 if rank is None else 1.0 / rank


def context_coverage(chunks: Sequence[str], golden_contexts: Sequence[str], k: int = 5) -> float:
    """
    Share of the golden contexts' word bigrams (pooled) found anywhere in the top-k chunks.
    Measures how much of the needed evidence reaches the LLM: a hit on a small fragment
    scores low, a chunk holding every golden context scores 1.0. No golden bigrams → 0.0.
    """
    golden = set().union(*(word_bigrams(g) for g in golden_contexts)) if golden_contexts else set()
    if not golden:
        return 0.0
    retrieved = set().union(*(word_bigrams(c) for c in chunks[:k])) if chunks else set()
    return len(golden & retrieved) / len(golden)


def hit_rate(ranks: Sequence[int | None]) -> float:
    """Share of questions with a first-relevant rank (i.e. a hit). [] → 0.0."""
    if not ranks:
        return 0.0
    return sum(rank is not None for rank in ranks) / len(ranks)


def mean_reciprocal_rank(ranks: Sequence[int | None]) -> float:
    """Mean of 1/rank over questions, counting a miss (None) as 0. [] → 0.0."""
    if not ranks:
        return 0.0
    return sum(0.0 if rank is None else 1.0 / rank for rank in ranks) / len(ranks)


def noise_rate(source_types_per_question: Sequence[Sequence[str]], k: int = 5) -> float:
    """
    Pooled share of top-k chunks whose source_type is "noisy". Questions that returned
    fewer than k chunks contribute only what they returned. No chunks at all → 0.0.
    """
    top = [source_type for types in source_types_per_question for source_type in types[:k]]
    if not top:
        return 0.0
    return sum(source_type == NOISY_SOURCE_TYPE for source_type in top) / len(top)
