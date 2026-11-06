import logfire

from app.agents.routing import MAX_RETRIEVAL_ATTEMPTS
from app.agents.state import AgentState
from app.services.retrieval.schema import (
    KNOWLEDGE_BASE_UNAVAILABLE_ANSWER,
    KNOWLEDGE_BASE_UNAVAILABLE_STATUS,
    RetrievalError,
    resolve_retrieval_mode,
)


def retrieve_node(state: AgentState):
    """
    Searches the knowledge base (hybrid dense + BM25 with RRF, or dense only, per
    RETRIEVAL_MODE) and reranks the candidates with FlashRank.
    Increments retrieval_attempts so the corrective loop is bounded.

    If the knowledge base can't be reached (RetrievalError) the request ends here
    with a "knowledge base unavailable" answer: an outage is not "no results", so it
    must not trigger the query-rewrite loop or the "not in the documents" reply.
    """
    # Imported lazily: these modules create Qdrant / embedding / FlashRank clients.
    from app.services.retrieval.qdrant_service import search_enterprise_knowledge
    from app.services.retrieval.ranking_service import rerank_records

    query = state["current_query"]
    attempt = state.get("retrieval_attempts", 0) + 1
    mode = resolve_retrieval_mode()
    label = f"Retrieval attempt {attempt}/{MAX_RETRIEVAL_ATTEMPTS} ({mode})"

    with logfire.span("🔍 Knowledge Retrieval", attempt=attempt, mode=mode):
        logfire.info(f"Searching Qdrant ({mode}) for: {query}")
        try:
            raw_results = search_enterprise_knowledge(query, limit=15, mode=mode)
        except RetrievalError as exc:
            logfire.error(f"Knowledge base unavailable: {exc}")
            return {
                "documents": [],
                "retrieval_attempts": attempt,
                "retrieval_error": str(exc),
                "final_answer": KNOWLEDGE_BASE_UNAVAILABLE_ANSWER,
                "grounded": None,
                "status": KNOWLEDGE_BASE_UNAVAILABLE_STATUS,
                "plan": state["plan"] + [f"{label}: knowledge base unavailable"],
                "messages": [{"role": "assistant", "content": KNOWLEDGE_BASE_UNAVAILABLE_ANSWER}],
            }
        logfire.info(f"Retrieved {len(raw_results)} candidates from Vector DB")

        with logfire.span("⚖️ Semantic Reranking"):
            reranked_records = rerank_records(query, raw_results, top_n=5)
            logfire.info(f"Reranking complete. Kept top {len(reranked_records)} chunks.")

    return {
        "documents": reranked_records,
        "retrieval_attempts": attempt,
        "status": "Found technical context." if reranked_records else "No matching context found.",
        "plan": state["plan"] + [f"{label}: {len(reranked_records)} chunks retrieved for '{query}'"],
    }
