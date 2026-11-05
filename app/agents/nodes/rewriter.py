"""Corrective RAG: rewrite a search query that retrieved nothing relevant."""

import logfire

from app.agents.llm import get_llm
from app.agents.routing import choose_rewritten_query
from app.agents.state import AgentState, latest_user_question


def build_rewrite_prompt(question: str, failed_query: str) -> str:
    return f"""A search over technical documentation (Kubernetes, Intel, networking) returned
no relevant results.

USER QUESTION:
"{question}"

FAILED SEARCH QUERY:
"{failed_query}"

Write ONE different search query that is more likely to match the documentation:
- use different wording and synonyms than the failed query
- expand acronyms (e.g. "HPA" -> "Horizontal Pod Autoscaler", "SR-IOV" -> "Single Root I/O Virtualization")
- keep the key technical terms, resource names and field names from the question

Reply with ONLY the new search query on one line."""


def rewrite_node(state: AgentState):
    question = latest_user_question(state)
    failed_query = state.get("current_query", "")

    with logfire.span("✏️ Query Rewrite", failed_query=failed_query):
        try:
            raw = get_llm("rewriter").invoke(build_rewrite_prompt(question, failed_query)).content
        except Exception as exc:
            logfire.error(f"Rewriter call failed, retrying with the original question: {exc}")
            raw = ""
        new_query = choose_rewritten_query(raw, failed_query=failed_query, original_question=question)
        logfire.info("Rewritten query", raw=raw, new_query=new_query)

    note = "" if new_query != question.strip() else " (using the original question)"
    return {
        "current_query": new_query,
        "status": f"Retrying search with: {new_query}",
        "plan": state["plan"] + [f"Query rewrite: '{failed_query}' → '{new_query}'{note}"],
    }
