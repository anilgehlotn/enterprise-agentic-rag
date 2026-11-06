import operator
from typing import Annotated, TypedDict


class AgentState(TypedDict):
    # Using Annotated with operator.add ensures that messages
    # are appended to the history rather than replaced.
    messages: Annotated[list[dict], operator.add]
    current_query: str
    documents: list[dict]
    plan: list[str]
    status: str
    final_answer: str
    # Corrective RAG: incremented by the retriever, capped by MAX_RETRIEVAL_ATTEMPTS.
    retrieval_attempts: int
    # Self-RAG: verifier verdict. None = not verified (conversational / no docs / unclear).
    grounded: bool | None
    # Set by the retriever when the knowledge base can't be searched; the graph then ends.
    retrieval_error: str | None


def new_request_state(question: str) -> dict:
    """
    Initial state for one /query call.

    The MemorySaver checkpointer keeps the whole state per thread_id between
    requests. Only `messages` should carry over (it has a reducer, so this new
    message is appended). Every per-request field is reset here, otherwise
    e.g. retrieval_attempts=2 from the previous question would skip the rewrite
    loop, or a stale `grounded` would be reported for a conversational answer.
    """
    return {
        "messages": [{"role": "user", "content": question}],
        "current_query": question,
        "documents": [],
        "plan": ["Start"],
        "status": "Initializing Graph...",
        "final_answer": "",
        "retrieval_attempts": 0,
        "grounded": None,
        "retrieval_error": None,
    }


def latest_user_question(state: dict) -> str:
    """The user's latest message, i.e. the question this run must answer."""
    for message in reversed(state.get("messages") or []):
        if message.get("role") == "user":
            return message.get("content", "")
    return ""


def conversation_history(state: dict) -> str:
    """All messages except the latest one, formatted for prompts."""
    lines = []
    for msg in (state.get("messages") or [])[:-1]:
        role = "User" if msg["role"] == "user" else "Assistant"
        lines.append(f"{role}: {msg['content']}")
    return "\n".join(lines)
