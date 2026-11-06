"""Corrective RAG: grade reranked chunks for relevance before answering.

The reranker orders chunks by similarity, but it always returns a top-N even
when none of them answers the question. One batched LLM call judges all chunks
at once (one call, not one per chunk) and only relevant ones are kept.
"""

import logfire

from app.agents.llm import get_llm
from app.agents.routing import parse_relevance_grades
from app.agents.state import AgentState, latest_user_question

# Caps prompt size: 5 chunks x 1,200 chars ≈ 1,500 input tokens per grading call.
GRADER_CHUNK_CHARS = 1_200


def build_grading_prompt(question: str, search_query: str, documents: list[dict]) -> str:
    chunks = "\n\n".join(
        f"[{index}] (source: {doc.get('source', 'unknown')})\n{doc.get('content', '')[:GRADER_CHUNK_CHARS]}"
        for index, doc in enumerate(documents)
    )
    return f"""You are grading retrieved documentation chunks for a RAG system.

USER QUESTION:
"{question}"

SEARCH QUERY USED:
"{search_query}"

CHUNKS (0-based index in brackets):
{chunks}

A chunk is relevant if it contains information that helps answer the user question,
even partially. Topical overlap alone (same product, different subject) is NOT relevant.

Reply with ONLY a JSON object listing the 0-based indices of the relevant chunks, e.g.
{{"relevant": [0, 2]}}
Use {{"relevant": []}} if none are relevant."""


def grade_node(state: AgentState):
    documents = state.get("documents") or []
    attempt = state.get("retrieval_attempts", 0)
    label = f"Grading (attempt {attempt})"

    if not documents:
        return {
            "documents": [],
            "status": "Nothing retrieved to grade.",
            "plan": state["plan"] + [f"{label}: no chunks to grade"],
        }

    prompt = build_grading_prompt(latest_user_question(state), state.get("current_query", ""), documents)

    with logfire.span("🧪 Relevance Grading", attempt=attempt, chunks=len(documents)):
        try:
            raw = get_llm("grader").invoke(prompt).content
        except Exception as exc:
            logfire.error(f"Grader call failed, keeping all chunks: {exc}")
            raw = None
        relevant = parse_relevance_grades(raw, len(documents))
        logfire.info("Grader verdict", raw=raw, relevant=relevant)

    if relevant is None:
        # Fail open: an unreadable grade must never discard evidence.
        return {
            "documents": documents,
            "status": "Grader output unreadable; keeping all chunks.",
            "plan": state["plan"]
            + [f"{label}: grader output unreadable, kept all {len(documents)} chunks (fail open)"],
        }

    kept = [documents[index] for index in relevant]
    return {
        "documents": kept,
        "status": f"{len(kept)} of {len(documents)} chunks relevant.",
        "plan": state["plan"] + [f"{label}: {len(kept)}/{len(documents)} chunks relevant"],
    }
