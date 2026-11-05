"""Pure retrieval metrics used by evals/retrieval_benchmark.py."""

import pytest

from evals.retrieval_metrics import (
    chunk_matches,
    first_relevant_rank,
    hit_rate,
    is_relevant,
    mean_reciprocal_rank,
    noise_rate,
    normalize,
    reciprocal_rank,
    token_overlap,
)

GOLDEN = "spec:\n  parallelism: 2\n  template:\n    metadata:\n      name: job-wq-2"
REDIS = ("For this example, you will start a single instance of Redis. Run the following "
         "commands: kubectl apply -f https://k8s.io/examples/application/job/redis/redis-pod.yaml")


# ── match rule ────────────────────────────────────────────────────────────────

def test_normalize_casefolds_and_collapses_whitespace():
    assert normalize("  Spec:\n\t  Parallelism:   2 \r\n") == "spec: parallelism: 2"
    assert normalize(None) == ""


def test_golden_inside_chunk_matches_despite_whitespace_and_case():
    chunk = "apiVersion: batch/v1\nkind: Job\nSPEC:    PARALLELISM: 2 TEMPLATE: METADATA: NAME: JOB-WQ-2\nmore text"
    assert chunk_matches(chunk, GOLDEN)


def test_chunk_inside_golden_matches():
    # The chunker split a golden passage: the chunk is a slice of the golden context.
    assert chunk_matches("parallelism: 2\n  template:", GOLDEN)


def test_token_overlap_tolerates_small_extraction_differences():
    extracted = REDIS.replace("Run the following commands:", "• Run the following commands —")
    assert REDIS not in extracted
    assert token_overlap(extracted, REDIS) >= 0.6
    assert chunk_matches(extracted, REDIS)


def test_shared_vocabulary_in_a_different_order_does_not_match():
    # Same YAML keywords, different manifest: unigram overlap would be high, bigram overlap is not.
    other_manifest = ("metadata: name: hello spec: schedule: '* * * * *' jobTemplate: template: spec: "
                      "containers: - name: hello parallelism: 5 job: wq")
    assert not chunk_matches(other_manifest, GOLDEN)


def test_overlap_just_below_threshold_does_not_match():
    golden = "alpha beta gamma delta epsilon zeta"        # 5 bigrams
    chunk = "alpha beta gamma delta | unrelated"           # 3 of 5 = 0.6 → match
    assert token_overlap(chunk, golden) == pytest.approx(0.6)
    assert chunk_matches(chunk, golden)
    assert not chunk_matches("alpha beta gamma | unrelated", golden)   # 2 of 5 = 0.4


@pytest.mark.parametrize("chunk, golden", [("", GOLDEN), (GOLDEN, ""), (None, GOLDEN), ("   ", "  ")])
def test_empty_text_never_matches(chunk, golden):
    assert chunk_matches(chunk, golden) is False


def test_very_short_golden_matches_only_by_containment():
    # Too few bigrams for an overlap score to mean anything.
    assert chunk_matches("set backoffLimit: 4 on the Job", "backoffLimit: 4")
    assert not chunk_matches("backoffLimit is 6 by default", "backoffLimit: 4")


def test_is_relevant_checks_every_golden_context():
    assert is_relevant(REDIS, ["something else entirely here", REDIS])
    assert not is_relevant(REDIS, [GOLDEN])
    assert not is_relevant(REDIS, [])


# ── hit rate / MRR ────────────────────────────────────────────────────────────

NOISE = ["Intel 5-level paging adds a fifth table level.", "C++ exceptions unwind the stack.",
         "100G NICs use SR-IOV virtual functions.", "Novice programmers forget semicolons."]


def test_hit_at_rank_1():
    chunks = [GOLDEN, *NOISE]
    assert first_relevant_rank(chunks, [GOLDEN]) == 1
    assert reciprocal_rank(chunks, [GOLDEN]) == 1.0


def test_hit_at_rank_3():
    chunks = [NOISE[0], NOISE[1], "prefix " + GOLDEN, NOISE[2], NOISE[3]]
    assert first_relevant_rank(chunks, [GOLDEN]) == 3
    assert reciprocal_rank(chunks, [GOLDEN]) == pytest.approx(1 / 3)


def test_no_hit():
    assert first_relevant_rank(NOISE, [GOLDEN]) is None
    assert reciprocal_rank(NOISE, [GOLDEN]) == 0.0


def test_match_beyond_k_is_not_a_hit():
    chunks = [*NOISE, "filler", GOLDEN]          # rank 6
    assert first_relevant_rank(chunks, [GOLDEN], k=5) is None
    assert first_relevant_rank(chunks, [GOLDEN], k=6) == 6


def test_aggregate_hit_rate_and_mrr():
    ranks = [1, 3, None, 2]
    assert hit_rate(ranks) == 0.75
    assert mean_reciprocal_rank(ranks) == pytest.approx((1 + 1 / 3 + 0 + 1 / 2) / 4)


def test_aggregates_of_no_questions_are_zero():
    assert hit_rate([]) == 0.0
    assert mean_reciprocal_rank([]) == 0.0


# ── noise rate ────────────────────────────────────────────────────────────────

def test_noise_rate_pools_top_k_chunks_over_questions():
    per_question = [
        ["true", "noisy", "true", "true", "noisy"],      # 2 noisy
        ["noisy", "noisy", "true", "true", "true"],      # 2 noisy
    ]
    assert noise_rate(per_question, k=5) == pytest.approx(4 / 10)


def test_noise_rate_only_counts_top_k_and_short_lists():
    assert noise_rate([["true", "true", "noisy"]], k=2) == 0.0           # noisy chunk is at rank 3
    assert noise_rate([["noisy"], ["true", "true", "true"]], k=5) == pytest.approx(1 / 4)


def test_noise_rate_ignores_other_source_types_and_handles_empty():
    assert noise_rate([["uploaded", "unknown", "true"]]) == 0.0
    assert noise_rate([[], []]) == 0.0
    assert noise_rate([]) == 0.0
