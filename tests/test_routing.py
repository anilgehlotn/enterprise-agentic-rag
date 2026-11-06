"""Unit tests for the pure routing / LLM-output parsing helpers in app/agents/routing.py."""

import pytest

from app.agents import routing
from app.agents.routing import (
    MAX_RETRIEVAL_ATTEMPTS,
    choose_rewritten_query,
    extract_json_object,
    parse_groundedness,
    parse_relevance_grades,
    route_after_grader,
    route_after_responder,
)


def test_retry_budget_is_two_attempts():
    assert MAX_RETRIEVAL_ATTEMPTS == 2


# ── extract_json_object ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"relevant": [0, 2]}', {"relevant": [0, 2]}),
        ('```json\n{"relevant": [1]}\n```', {"relevant": [1]}),
        ('Sure! Here you go: {"grounded": true} Hope that helps.', {"grounded": True}),
        # Braces inside prose before the real object are skipped.
        ('Chunks {0..4} reviewed. Result: {"relevant": []}', {"relevant": []}),
    ],
)
def test_extract_json_object_finds_the_object(raw, expected):
    assert extract_json_object(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "no json here", "[0, 1]", '{"relevant": [0,'])
def test_extract_json_object_returns_none_without_an_object(raw):
    assert extract_json_object(raw) is None


# ── parse_relevance_grades ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"relevant": [0, 2]}', [0, 2]),
        ('{"relevant": [2, 0, 2]}', [0, 2]),  # sorted, de-duplicated
        ('{"relevant": []}', []),  # explicit "none relevant"
        ('{"relevant": ["1", 3]}', [1, 3]),  # numeric strings accepted
        ('The relevant chunks are below.\n{"relevant": [4]}\nThanks!', [4]),  # wrapped in prose
        ('```json\n{"relevant": [0, 1]}\n```', [0, 1]),  # fenced
    ],
)
def test_parse_relevance_grades_valid(raw, expected):
    assert parse_relevance_grades(raw, num_documents=5) == expected


def test_parse_relevance_grades_ignores_out_of_range_and_junk_entries():
    raw = '{"relevant": [-1, 1, 5, 99, "x", null, true, 2.5, 3]}'

    assert parse_relevance_grades(raw, num_documents=5) == [1, 3]


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "Chunks 0 and 2 are relevant.",  # prose, no JSON
        '{"relevant": [0, 2',  # truncated JSON
        '{"chunks": [0]}',  # wrong key
        '{"relevant": "0, 2"}',  # not a list
        '{"relevant": [5, 7]}',  # non-empty but every index out of range
        '{"relevant": [true]}',  # bool is not an index
    ],
)
def test_parse_relevance_grades_unreadable_returns_none_to_fail_open(raw):
    assert parse_relevance_grades(raw, num_documents=5) is None


# ── route_after_grader ────────────────────────────────────────────────────────


def test_route_after_grader_answers_when_relevant_docs_remain():
    assert route_after_grader({"documents": [{"content": "x"}], "retrieval_attempts": 1}) == routing.RESPONDER


@pytest.mark.parametrize("attempts", [0, 1])
def test_route_after_grader_rewrites_while_under_budget(attempts):
    assert route_after_grader({"documents": [], "retrieval_attempts": attempts}) == routing.REWRITER


@pytest.mark.parametrize("attempts", [2, 3])
def test_route_after_grader_gives_up_at_the_budget(attempts):
    assert route_after_grader({"documents": [], "retrieval_attempts": attempts}) == routing.RESPONDER


def test_route_after_grader_treats_missing_fields_as_first_failure():
    assert route_after_grader({}) == routing.REWRITER


# ── choose_rewritten_query ────────────────────────────────────────────────────

ORIGINAL = "How do I limit retries for a failing Job?"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Kubernetes Job backoffLimit pod failure policy", "Kubernetes Job backoffLimit pod failure policy"),
        ('"Horizontal Pod Autoscaler scaling metrics"', "Horizontal Pod Autoscaler scaling metrics"),
        ("Rewritten query: Single Root I/O Virtualization setup", "Single Root I/O Virtualization setup"),
        ("Query: job retry limit\nThis wording uses synonyms.", "job retry limit"),
        ("\n\n  job restartPolicy backoffLimit  \n", "job restartPolicy backoffLimit"),
    ],
)
def test_choose_rewritten_query_cleans_the_rewrite(raw, expected):
    assert choose_rewritten_query(raw, failed_query="job retries", original_question=ORIGINAL) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        '""',
        "job retries",  # identical to the failed query
        "  JOB   Retries. ",  # identical ignoring case/whitespace/quotes
        '"job retries"',
        "CONVERSATIONAL",  # must never turn the search into the routing sentinel
    ],
)
def test_choose_rewritten_query_falls_back_to_original_question(raw):
    result = choose_rewritten_query(raw, failed_query="job retries", original_question=f"  {ORIGINAL} ")

    assert result == ORIGINAL


# ── route_after_responder ─────────────────────────────────────────────────────


def test_route_after_responder_verifies_technical_answers_with_evidence():
    state = {"current_query": "job backoffLimit", "documents": [{"content": "x"}]}

    assert route_after_responder(state) == routing.VERIFIER


@pytest.mark.parametrize(
    "state",
    [
        {"current_query": "CONVERSATIONAL", "documents": []},
        {"current_query": "CONVERSATIONAL", "documents": [{"content": "stale"}]},
        {"current_query": "job backoffLimit", "documents": []},  # "not in the documents" answer
        {"current_query": "job backoffLimit"},
    ],
)
def test_route_after_responder_skips_verifier(state):
    assert route_after_responder(state) == routing.END


# ── parse_groundedness ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        '{"grounded": true}',
        '{"grounded": "yes"}',
        '{"grounded": "True"}',
        'Checked every claim. {"grounded": true}',
        '```json\n{"grounded": true}\n```',
        "yes",
        "Yes.",
        "true",
    ],
)
def test_parse_groundedness_yes(raw):
    assert parse_groundedness(raw) is True


@pytest.mark.parametrize(
    "raw",
    ['{"grounded": false}', '{"grounded": "no"}', 'Verdict: {"grounded": false}', "no", "False"],
)
def test_parse_groundedness_no(raw):
    assert parse_groundedness(raw) is False


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        '{"grounded": "partially"}',
        '{"grounded": null}',
        '{"supported": true}',
        '{"grounded": 1}',
        "Mostly supported, but claim 2 is unclear.",
        "yes and no",
        '{"grounded": tru',
    ],
)
def test_parse_groundedness_unclear_is_none(raw):
    assert parse_groundedness(raw) is None
