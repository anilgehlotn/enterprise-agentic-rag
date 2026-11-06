"""LangGraph wiring for the RAG agent.

Self-correcting graph (Corrective RAG + Self-RAG), the default:

    planner ─CONVERSATIONAL─────────────────────────────▶ responder ─▶ END
    planner ─technical─▶ retriever ─▶ grader ─relevant──▶ responder ─▶ verifier ─▶ END
                             ▲  │       │
                             │  │       ├─none, attempts < 2─▶ rewriter ─┐
                             └──┼───────┼────────────────────────────────┘
                                │       └─none, attempts ≥ 2─▶ responder ("not in the documents") ─▶ END
                                └─knowledge base unavailable (RetrievalError)─▶ END

Linear graph (ENABLE_SELF_CORRECTION=false), the original pipeline:

    planner ─▶ (retriever) ─▶ responder ─▶ END      (retriever ─unavailable─▶ END)

Token cost of self-correction (per technical question, vs. the linear graph):
  - always:            +1 grader LLM call (one batched call for all chunks, ~1.5k input
                       tokens) and +1 verifier LLM call (answer + evidence),
                       so 2 extra calls on top of planner + responder.
  - only when grading
    finds nothing:     +1 rewriter LLM call (small) and +1 more retrieval
                       (embedding + Qdrant + FlashRank, no LLM tokens) and +1 more
                       grader call. When the retry also finds nothing, the responder
                       returns a fixed "not in the documents" reply and the
                       responder and verifier LLM calls are skipped.
  - conversational:    no extra calls (grader/rewriter/verifier never run).
"""

import os
from functools import lru_cache

from langgraph.graph import END, StateGraph

from app.agents import routing
from app.agents.state import AgentState


def self_correction_enabled(value: str | None = None) -> bool:
    """Reads ENABLE_SELF_CORRECTION (default on). Only false/0/no/off disables it."""
    if value is None:
        value = os.getenv("ENABLE_SELF_CORRECTION", "true")
    return value.strip().lower() not in {"false", "0", "no", "off"}


def build_graph(
    *,
    planner=None,
    retriever=None,
    grader=None,
    rewriter=None,
    responder=None,
    verifier=None,
    checkpointer=None,
    self_correction: bool = True,
):
    """
    Builds and compiles the agent graph.

    Every node is injectable so tests can pass fakes; a node left as None uses
    the production implementation. grader/rewriter/verifier are ignored when
    self_correction=False.
    """
    if planner is None:
        from app.agents.nodes.planner import planner_node as planner
    if retriever is None:
        from app.agents.nodes.retriever import retrieve_node as retriever
    if responder is None:
        from app.agents.nodes.responder import generate_node as responder

    workflow = StateGraph(AgentState)
    workflow.add_node(routing.RETRIEVER, retriever)
    workflow.add_node(routing.RESPONDER, responder)
    workflow.add_node("planner", planner)
    workflow.set_entry_point("planner")

    # planner → responder (conversational) | retriever (technical)
    workflow.add_conditional_edges(
        "planner",
        routing.route_planner,
        {routing.RETRIEVER: routing.RETRIEVER, routing.RESPONDER: routing.RESPONDER},
    )

    if not self_correction:
        # retriever → responder | END (knowledge base unavailable)
        workflow.add_conditional_edges(
            routing.RETRIEVER,
            lambda state: routing.route_after_retriever(state, routing.RESPONDER),
            {routing.RESPONDER: routing.RESPONDER, routing.END: END},
        )
        workflow.add_edge(routing.RESPONDER, END)
        return workflow.compile(checkpointer=checkpointer)

    if grader is None:
        from app.agents.nodes.grader import grade_node as grader
    if rewriter is None:
        from app.agents.nodes.rewriter import rewrite_node as rewriter
    if verifier is None:
        from app.agents.nodes.verifier import verify_node as verifier

    workflow.add_node("grader", grader)
    workflow.add_node(routing.REWRITER, rewriter)
    workflow.add_node(routing.VERIFIER, verifier)

    # retriever → grader | END (knowledge base unavailable)
    workflow.add_conditional_edges(
        routing.RETRIEVER,
        lambda state: routing.route_after_retriever(state, "grader"),
        {"grader": "grader", routing.END: END},
    )
    # grader → responder (relevant docs, or retries exhausted) | rewriter (try again)
    workflow.add_conditional_edges(
        "grader",
        routing.route_after_grader,
        {routing.RESPONDER: routing.RESPONDER, routing.REWRITER: routing.REWRITER},
    )
    workflow.add_edge(routing.REWRITER, routing.RETRIEVER)
    # responder → verifier (technical answer with evidence) | END
    workflow.add_conditional_edges(
        routing.RESPONDER,
        routing.route_after_responder,
        {routing.VERIFIER: routing.VERIFIER, routing.END: END},
    )
    workflow.add_edge(routing.VERIFIER, END)

    return workflow.compile(checkpointer=checkpointer)


@lru_cache(maxsize=1)
def get_rag_agent():
    """
    Production graph: real nodes + the process checkpointer (memory per thread_id;
    backend chosen by CHECKPOINTER, see app/agents/checkpointer.py). Built once per process.
    """
    from app.agents.checkpointer import get_checkpointer

    return build_graph(checkpointer=get_checkpointer(), self_correction=self_correction_enabled())


def shutdown_rag_agent() -> None:
    """Closes the checkpointer connection and drops the cached graph that holds it."""
    from app.agents.checkpointer import close_checkpointer

    try:
        close_checkpointer()  # first: the connection must be released even if the rest fails
    finally:
        get_rag_agent.cache_clear()
