import logfire
from app.agents.state import AgentState
from app.services.retrieval.qdrant_service import search_enterprise_knowledge
from app.services.retrieval.ranking_service import rerank_records

def retrieve_node(state: AgentState):
    """
    Performs vector search and semantic reranking for technical queries.
    """
    query = state["current_query"]
    
    
    # Standard Retrieval Logic
    with logfire.span("🔍 Knowledge Retrieval"):
        logfire.info(f"Searching Qdrant for: {query}")
        raw_results = search_enterprise_knowledge(query, limit=15)
        logfire.info(f"Retrieved {len(raw_results)} candidates from Vector DB")
        
        with logfire.span("⚖️ Semantic Reranking"):
            reranked_records = rerank_records(query, raw_results, top_n=5)
            logfire.info("Reranking complete. Kept top 5 most relevant chunks.")

    return {
        "documents": reranked_records,
        "status": f"Found technical context.",
        "plan": state["plan"] + ["Context Retrieved"]
    }
