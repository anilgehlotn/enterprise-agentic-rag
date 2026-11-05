"""Shared RAG service layer used by every entry point (FastAPI /query and the MCP server).

Keeping the guardrails → agent flow here means the HTTP API and MCP tools can't
drift apart: a fix or a new safety check lands in both at once.

Imports of guardrails, the agent graph, Qdrant and FlashRank are lazy (inside the
functions), so importing this module needs no API keys, network or heavy packages.
"""

import json
import os
from pathlib import Path
from typing import Any

import logfire

from app.services.retrieval.citations import build_citations

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Same answer the API has always returned when the pipeline raises.
INTERNAL_ERROR_ANSWER = (
    "I apologize, but I encountered an internal error while processing your request. "
    "Please try again later."
)


def ensure_rails() -> None:
    """Initialise NeMo Guardrails once per process (lazy, idempotent, thread-safe)."""
    from app.guardrails import ensure_rails as _ensure_rails

    _ensure_rails()


def answer_question(question: str, thread_id: str | None) -> dict[str, Any]:
    """
    Guardrails gate + self-correcting LangGraph agent.

    Returns the QueryResponse shape: question, answer, thought_process, status,
    sources, grounded. Never raises: pipeline errors become status="error".
    """
    from app.agents.graph import get_rag_agent
    from app.agents.state import new_request_state

    # Resets every per-request field (retrieval_attempts, grounded, documents, plan…)
    # so values checkpointed by MemorySaver for this thread don't leak into this run.
    initial_state = new_request_state(question)

    # Configuration for Memory (Thread ID)
    config = {"configurable": {"thread_id": thread_id}}

    try:
        # Gate 1: NeMo Guardrails — blocks off-topic, jailbreaks, and handles dialog.
        # Initialised here (not only at FastAPI startup) so every entry point is guarded.
        # If initialisation fails the request fails closed (status="error"), never unguarded.
        ensure_rails()
        from app.guardrails import guard

        rail_fired, rail_response = guard(question)
        if rail_fired:
            logfire.info(f"🛡️ Request blocked by guardrails | thread={thread_id}")
            return {
                "question": question,
                "answer": rail_response,
                "thought_process": ["Intent: Guardrails Fired", "Retrieval: Skipped"],
                "status": "Blocked by guardrails.",
                "sources": [],
                "grounded": None,
            }

        # Gate 2: LangGraph RAG pipeline
        # Run the graph synchronously to preserve Logfire context variables
        final_output = get_rag_agent().invoke(initial_state, config=config)

        return {
            "question": question,
            "answer": final_output.get("final_answer"),
            "thought_process": final_output.get("plan"),
            "status": final_output.get("status"),
            "sources": build_citations(final_output.get("documents", [])),
            "grounded": final_output.get("grounded"),
        }
    except Exception as e:
        logfire.error(f"❌ Backend Execution Failed: {e}")
        return {
            "question": question,
            "answer": INTERNAL_ERROR_ANSWER,
            "thought_process": ["Error encountered during execution."],
            "status": "error",
            "sources": [],
            "grounded": None,
        }


def search_knowledge(query: str, top_k: int) -> list[dict[str, Any]]:
    """
    Vector search (Qdrant) + FlashRank rerank, formatted with build_citations().
    No LLM call: cheap, fast, deterministic evidence lookup.
    """
    from app.services.retrieval.qdrant_service import search_enterprise_knowledge
    from app.services.retrieval.ranking_service import rerank_records

    with logfire.span("🔍 Knowledge Search (no LLM)", top_k=top_k):
        # Over-fetch so the cross-encoder has candidates to reorder (the agent uses 15 → 5).
        candidates = search_enterprise_knowledge(query, limit=max(15, top_k * 3))
        reranked = rerank_records(query, candidates, top_n=top_k)
        return build_citations(reranked)


def processed_data_dir() -> Path:
    """Where ingestion writes processed_data/<source_type>/<file>.json (override with PROCESSED_DATA_DIR)."""
    configured = os.getenv("PROCESSED_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    # Resolved from the project root, not the CWD: MCP clients such as Claude
    # Desktop launch the server from an arbitrary working directory.
    return PROJECT_ROOT / "processed_data"


def list_documents(base_dir: str | os.PathLike | None = None) -> list[dict[str, Any]]:
    """
    Indexed documents with chunk counts, read from processed_data/<source_type>/*.json.
    Unreadable files are skipped (and logged), never fatal.
    """
    root = Path(base_dir) if base_dir is not None else processed_data_dir()
    if not root.is_dir():
        return []

    documents = []
    for path in sorted(root.glob("*/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logfire.warning(f"Skipping unreadable processed file {path.name}: {exc}")
            continue
        if not isinstance(data, dict):
            continue
        chunks = data.get("chunks")
        documents.append(
            {
                "filename": data.get("filename") or path.name.removesuffix(".json"),
                "source_type": data.get("source_type") or path.parent.name,
                "chunk_count": len(chunks) if isinstance(chunks, list) else 0,
            }
        )

    return sorted(documents, key=lambda d: (d["source_type"], d["filename"].casefold()))
