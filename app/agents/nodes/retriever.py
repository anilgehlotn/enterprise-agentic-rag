import logfire
from app.agents.routing import MAX_RETRIEVAL_ATTEMPTS
from app.agents.state import AgentState


def retrieve_node(state: AgentState):
    """
    Performs vector search and semantic reranking for technical queries.
    Increments retrieval_attempts so the corrective loop is bounded.
    """
    # Imported lazily: these modules create Qdrant / embedding / FlashRank clients.
    from app.services.retrieval.qdrant_service import search_enterprise_knowledge
    from app.services.retrieval.ranking_service import rerank_records

    query = state["current_query"]
    attempt = state.get("retrieval_attempts", 0) + 1

    with logfire.span("🔍 Knowledge Retrieval", attempt=attempt):
        logfire.info(f"Searching Qdrant for: {query}")
        raw_results = search_enterprise_knowledge(query, limit=15)
        logfire.info(f"Retrieved {len(raw_results)} candidates from Vector DB")

        with logfire.span("⚖️ Semantic Reranking"):
            reranked_records = rerank_records(query, raw_results, top_n=5)
            logfire.info(f"Reranking complete. Kept top {len(reranked_records)} chunks.")

    return {
        "documents": reranked_records,
        "retrieval_attempts": attempt,
        "status": "Found technical context." if reranked_records else "No matching context found.",
        "plan": state["plan"] + [
            f"Retrieval attempt {attempt}/{MAX_RETRIEVAL_ATTEMPTS}: "
            f"{len(reranked_records)} chunks retrieved for '{query}'"
        ],
    }
