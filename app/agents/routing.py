"""Pure routing helpers for the planner. No LLM, network, or LangGraph imports,
so these can be unit-tested in isolation."""

CONVERSATIONAL = "CONVERSATIONAL"

# Characters an LLM commonly wraps around a one-word answer.
_WRAPPING_CHARS = " \t\r\n\"'`*“”‘’"
_TRAILING_PUNCTUATION = ".!?,;:"


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
    """LangGraph conditional edge: skip retrieval for conversational turns."""
    if is_conversational(state.get("current_query")):
        return "responder"
    return "retriever"
