"""Pure routing decisions and LLM-output parsing for the agent graph.

No LLM, network, or LangGraph imports, so everything here is unit-testable in
isolation. Nodes call the parsers; graph.py wires the route_* functions in as
conditional edges.
"""

import json
import re

CONVERSATIONAL = "CONVERSATIONAL"

# Corrective-RAG budget: the first retrieval plus at most one rewrite-and-retry.
MAX_RETRIEVAL_ATTEMPTS = 2

# Route labels returned by the route_* functions (graph.py maps them to nodes).
RETRIEVER = "retriever"
REWRITER = "rewriter"
RESPONDER = "responder"
VERIFIER = "verifier"
END = "end"

# Characters an LLM commonly wraps around a one-word answer.
_WRAPPING_CHARS = " \t\r\n\"'`*“”‘’"
_TRAILING_PUNCTUATION = ".!?,;:"
_QUERY_LABEL = re.compile(r"^(?:rewritten\s+|new\s+|improved\s+)?(?:search\s+)?query\s*:\s*", re.IGNORECASE)


# ── Planner ────────────────────────────────────────────────────────────────────


def normalize_planner_decision(raw: str | None) -> str:
    """
    Cleans the planner LLM output.

    Returns the CONVERSATIONAL sentinel when the model meant it, even if it was
    decorated ("CONVERSATIONAL.", '"CONVERSATIONAL"', "**conversational**").
    Otherwise returns the search query with surrounding quotes/whitespace removed.
    """
    text = (raw or "").strip().strip(_WRAPPING_CHARS)
    token = text.rstrip(_TRAILING_PUNCTUATION).strip(_WRAPPING_CHARS)
    if token.upper() == CONVERSATIONAL:
        return CONVERSATIONAL
    return text


def is_conversational(decision: str | None) -> bool:
    return normalize_planner_decision(decision) == CONVERSATIONAL


def route_planner(state: dict) -> str:
    """planner → responder (conversational) | retriever (technical)."""
    if is_conversational(state.get("current_query")):
        return RESPONDER
    return RETRIEVER


# ── JSON extraction shared by the grader and verifier parsers ─────────────────


def extract_json_object(raw: str | None) -> dict | None:
    """
    Returns the first JSON object found in an LLM reply, or None.
    Handles bare JSON, ```json fences, and JSON embedded in prose
    ('Sure! Here is the result: {"relevant": [0]} Hope that helps').
    """
    if not raw:
        return None
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", raw):
        try:
            value, _ = decoder.raw_decode(raw, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


# ── Retriever ──────────────────────────────────────────────────────────────────


def route_after_retriever(state: dict, next_node: str) -> str:
    """
    retriever → END when the knowledge base was unreachable (the retriever already
    wrote the "unavailable" answer); → next_node (grader, or responder in the linear
    graph) otherwise. An outage must not look like "no results" and trigger a rewrite.
    """
    if state.get("retrieval_error"):
        return END
    return next_node


# ── Grader ─────────────────────────────────────────────────────────────────────


def _as_index(value) -> int | None:
    if isinstance(value, bool):  # bool is a subclass of int; True must not mean chunk 1
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_relevance_grades(raw: str | None, num_documents: int) -> list[int] | None:
    """
    Parses the grader reply {"relevant": [0, 2]} into sorted, de-duplicated,
    in-range chunk indices.

    Returns None when the reply can't be trusted, and the caller then keeps
    ALL chunks (fail open):
      - no JSON object / no "relevant" key / "relevant" is not a list
      - the list is non-empty but none of its entries is a valid index
        (malformed output, e.g. 1-based indices for a single chunk), which is
        different from the model deliberately answering [].
    Out-of-range or non-integer entries are ignored when at least one is valid.
    An explicit empty list means "nothing relevant" and returns [].
    """
    data = extract_json_object(raw)
    if data is None or "relevant" not in data:
        return None
    entries = data["relevant"]
    if not isinstance(entries, list):
        return None
    if not entries:
        return []

    indices = {
        index for index in (_as_index(entry) for entry in entries) if index is not None and 0 <= index < num_documents
    }
    if not indices:
        return None
    return sorted(indices)


def route_after_grader(state: dict) -> str:
    """
    grader → responder when relevant evidence survived grading;
           → rewriter for another attempt while under MAX_RETRIEVAL_ATTEMPTS;
           → responder (which then admits the documents don't cover it) otherwise.
    """
    if state.get("documents"):
        return RESPONDER
    if state.get("retrieval_attempts", 0) < MAX_RETRIEVAL_ATTEMPTS:
        return REWRITER
    return RESPONDER


# ── Rewriter ───────────────────────────────────────────────────────────────────


def _squash(text: str) -> str:
    """Comparison key: case, whitespace, quotes and trailing punctuation don't change a search."""
    return " ".join(text.split()).strip(_WRAPPING_CHARS).rstrip(_TRAILING_PUNCTUATION).casefold()


def choose_rewritten_query(raw: str | None, failed_query: str | None, original_question: str) -> str:
    """
    Cleans the rewriter reply (first non-empty line, no quotes or 'Query:' label).
    Falls back to the user's original question if the rewrite is empty, the
    CONVERSATIONAL sentinel, or identical (ignoring case/whitespace) to the
    query that just failed.
    """
    lines = [line for line in (raw or "").splitlines() if line.strip(_WRAPPING_CHARS)]
    candidate = lines[0].strip(_WRAPPING_CHARS) if lines else ""
    candidate = _QUERY_LABEL.sub("", candidate).strip(_WRAPPING_CHARS)

    if not candidate or is_conversational(candidate) or _squash(candidate) == _squash(failed_query or ""):
        return original_question.strip()
    return candidate


# ── Responder / verifier ──────────────────────────────────────────────────────


def route_after_responder(state: dict) -> str:
    """
    responder → verifier only for technical answers that cite evidence.
    Conversational answers and "no documents cover this" answers skip it.
    """
    if is_conversational(state.get("current_query")) or not state.get("documents"):
        return END
    return VERIFIER


_YES = {"true", "yes", "grounded", "supported"}
_NO = {"false", "no", "ungrounded", "unsupported", "not grounded", "not supported"}


def _as_verdict(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().strip(_WRAPPING_CHARS).rstrip(_TRAILING_PUNCTUATION).casefold()
        if token in _YES:
            return True
        if token in _NO:
            return False
    return None


def parse_groundedness(raw: str | None) -> bool | None:
    """
    Parses the verifier reply {"grounded": true|false}.
    Also accepts "yes"/"no" strings inside the JSON, or a bare one-word reply.
    Returns None when the verdict is unclear ("partially", prose, missing key).
    """
    data = extract_json_object(raw)
    if data is not None:
        return _as_verdict(data.get("grounded"))
    return _as_verdict(raw or "")
