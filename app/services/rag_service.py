"""Shared RAG service layer used by every entry point (FastAPI /query and the MCP server).

Keeping the guardrails → agent flow here means the HTTP API and MCP tools can't
drift apart: a fix or a new safety check lands in both at once.

Imports of guardrails, the agent graph, Qdrant and FlashRank are lazy (inside the
functions), so importing this module needs no API keys, network or heavy packages.
"""

import json
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import logfire

from app import analytics
from app.services.retrieval.citations import build_citations

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Same answer the API has always returned when the pipeline raises.
INTERNAL_ERROR_ANSWER = (
    "I apologize, but I encountered an internal error while processing your request. Please try again later."
)


def ensure_rails() -> None:
    """Initialise NeMo Guardrails once per process (lazy, idempotent, thread-safe)."""
    from app.guardrails import ensure_rails as _ensure_rails

    _ensure_rails()


# ── Shared steps of /query and /query/stream ─────────────────────────────────
# Both entry points run exactly these helpers, so guardrails, state reset, thread
# config and the response shape can't drift between streaming and non-streaming.


def _check_guardrails(question: str, thread_id: str | None) -> dict[str, Any] | None:
    """
    Gate 1: NeMo Guardrails — blocks off-topic, jailbreaks, and handles dialog.
    Returns the blocked-response dict, or None when the question may proceed.

    Initialised here (not only at FastAPI startup) so every entry point is guarded.
    If initialisation fails this raises, and the caller fails closed (error), never unguarded.
    """
    ensure_rails()
    from app.guardrails import guard

    rail_fired, rail_response = guard(question)
    if not rail_fired:
        return None
    logfire.info(f"🛡️ Request blocked by guardrails | thread={thread_id}")
    return {
        "question": question,
        "answer": rail_response,
        "thought_process": ["Intent: Guardrails Fired", "Retrieval: Skipped"],
        "status": "Blocked by guardrails.",
        "sources": [],
        "grounded": None,
    }


def _agent_inputs(question: str, thread_id: str | None, **configurable) -> tuple[dict, dict]:
    """Initial state + run config for one question on one conversation thread."""
    from app.agents.state import new_request_state

    # Resets every per-request field (retrieval_attempts, grounded, documents, plan…)
    # so values checkpointed for this thread don't leak into this run. Only `messages`
    # carries over: that is the conversation memory.
    initial_state = new_request_state(question)
    # thread_id selects which checkpointed conversation this run continues.
    config = {"configurable": {"thread_id": thread_id, **configurable}}
    return initial_state, config


def _result_from_state(question: str, final_output: dict) -> dict[str, Any]:
    return {
        "question": question,
        "answer": final_output.get("final_answer"),
        "thought_process": final_output.get("plan"),
        "status": final_output.get("status"),
        "sources": build_citations(final_output.get("documents", [])),
        "grounded": final_output.get("grounded"),
    }


def _error_result(question: str) -> dict[str, Any]:
    return {
        "question": question,
        "answer": INTERNAL_ERROR_ANSWER,
        "thought_process": ["Error encountered during execution."],
        "status": "error",
        "sources": [],
        "grounded": None,
    }


# ── Query analytics (app/analytics) ──────────────────────────────────────────
# Every entry point (/query = "api", /query/stream = "stream", MCP = "mcp") logs one
# query_log row through these two helpers. Logging only enqueues a row for a background
# writer: it never touches the database on the request thread and never raises.


def _start_query() -> tuple[str | None, float]:
    """A query id (None when analytics is off or misconfigured) and the start time."""
    try:
        query_id = analytics.new_query_id()
    except Exception as exc:
        logfire.error(f"Analytics disabled for this request: {exc}")
        query_id = None
    return query_id, time.perf_counter()


def _finish_query(
    result: dict[str, Any],
    query_id: str | None,
    started: float,
    source: str,
    question: str,
    thread_id: str | None,
    retrieval_attempts: int = 0,
) -> dict[str, Any]:
    """Adds query_id to the response and queues the analytics row."""
    result["query_id"] = query_id
    latency_ms = (time.perf_counter() - started) * 1000
    analytics.log_query(query_id, source, question, thread_id, result, retrieval_attempts, latency_ms)
    return result


def answer_question(question: str, thread_id: str | None, source: str = "api") -> dict[str, Any]:
    """
    Guardrails gate + self-correcting LangGraph agent.

    Returns the QueryResponse shape: question, answer, thought_process, status,
    sources, grounded, query_id. Never raises: pipeline errors become status="error".
    source ("api" or "mcp") is recorded in query analytics.
    """
    from app.agents.graph import get_rag_agent

    query_id, started = _start_query()
    try:
        blocked = _check_guardrails(question, thread_id)
        if blocked is not None:
            return _finish_query(blocked, query_id, started, source, question, thread_id)

        # Gate 2: LangGraph RAG pipeline
        # Run the graph synchronously to preserve Logfire context variables
        initial_state, config = _agent_inputs(question, thread_id)
        final_output = get_rag_agent().invoke(initial_state, config=config)
        result = _result_from_state(question, final_output)
        attempts = final_output.get("retrieval_attempts") or 0
        return _finish_query(result, query_id, started, source, question, thread_id, attempts)
    except Exception as e:
        logfire.error(f"❌ Backend Execution Failed: {e}")
        return _finish_query(_error_result(question), query_id, started, source, question, thread_id)


# ── Streaming ─────────────────────────────────────────────────────────────────

# Server-Sent Event types emitted by stream_answer(), in order:
#   step*    one per new plan entry as each graph node finishes
#   token*   answer text chunks
#   sources  final citations
#   done     status, grounded, thought_process, query_id (+ the full answer)
# or, instead of sources/done when something fails:
#   error    friendly message, never a traceback
STEP, TOKEN, SOURCES, DONE, ERROR = "step", "token", "sources", "done", "error"

StreamEvent = tuple[str, dict[str, Any]]


def _text_chunks(text: str) -> list[str]:
    """Splits a ready-made reply into word-sized chunks so it renders like a streamed one."""
    return re.findall(r"\S+\s*|\s+", text or "")


def _steps_from_update(node: str, update: dict, previous_plan: list[str]) -> list[dict[str, Any]]:
    """
    Step events for one node's state update. Nodes return the whole plan list
    (previous entries + new ones); only the new entries become events. The planner
    starts a fresh list, which then is entirely new. A node that adds no plan entry
    (e.g. a plain responder answer) still reports that it finished, with its status.
    """
    status = update.get("status")
    plan = update.get("plan")
    if plan is None:
        new_entries = []
    elif plan[: len(previous_plan)] == previous_plan:
        new_entries = plan[len(previous_plan) :]
    else:
        new_entries = plan
    if not new_entries:
        return [{"node": node, "step": status or f"{node} finished", "status": status}]
    return [{"node": node, "step": entry, "status": status} for entry in new_entries]


def stream_answer(question: str, thread_id: str | None) -> Iterator[StreamEvent]:
    """
    Same guardrails + agent path as answer_question(), yielded as (event, data) pairs
    while it runs. Never raises: a failure becomes one final `error` event.

    stream_mode=["updates", "custom"]: "updates" yields each node's state update as the
    node finishes (→ step events); "custom" yields what nodes push through LangGraph's
    stream writer — here the responder's LLM tokens (→ token events).
    """
    from app.agents.graph import get_rag_agent
    from app.agents.streaming import STREAM_TOKENS_KEY, TOKEN_ITEM

    query_id, started = _start_query()
    try:
        blocked = _check_guardrails(question, thread_id)
        if blocked is not None:
            _finish_query(blocked, query_id, started, "stream", question, thread_id)
            for chunk in _text_chunks(blocked["answer"]):
                yield TOKEN, {"text": chunk}
            yield DONE, _done_payload(blocked)
            return

        initial_state, config = _agent_inputs(question, thread_id, **{STREAM_TOKENS_KEY: True})
        # Latest value of every field, rebuilt from the per-node updates (messages
        # excluded: the response doesn't need them and the checkpointer stores them).
        final_state = {key: value for key, value in initial_state.items() if key != "messages"}
        streamed_tokens = False

        for mode, chunk in get_rag_agent().stream(initial_state, config=config, stream_mode=["updates", "custom"]):
            if mode == "custom":
                if isinstance(chunk, dict) and chunk.get("type") == TOKEN_ITEM and chunk.get("text"):
                    streamed_tokens = True
                    yield TOKEN, {"text": chunk["text"]}
                continue
            for node, update in (chunk or {}).items():
                if not isinstance(update, dict):
                    continue
                yield from ((STEP, step) for step in _steps_from_update(node, update, final_state.get("plan") or []))
                final_state.update({key: value for key, value in update.items() if key != "messages"})

        result = _result_from_state(question, final_state)
        attempts = final_state.get("retrieval_attempts") or 0
        _finish_query(result, query_id, started, "stream", question, thread_id, attempts)
        if not streamed_tokens:
            # Answers written without an LLM call: "knowledge base unavailable"
            # (retriever) and "not in the documents" (responder).
            for text in _text_chunks(result["answer"]):
                yield TOKEN, {"text": text}
        yield SOURCES, {"sources": result["sources"]}
        yield DONE, _done_payload(result)
    except Exception as e:
        logfire.error(f"❌ Streaming Execution Failed: {e}")
        _finish_query(_error_result(question), query_id, started, "stream", question, thread_id)
        yield ERROR, {"message": INTERNAL_ERROR_ANSWER}


def _done_payload(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result["status"],
        "grounded": result["grounded"],
        "thought_process": result["thought_process"],
        "answer": result["answer"],
        "query_id": result.get("query_id"),
    }


# ── Conversation memory ───────────────────────────────────────────────────────


def reset_thread(thread_id: str) -> None:
    """Deletes every checkpoint of `thread_id`: the next question starts a fresh conversation."""
    from app.agents.checkpointer import get_checkpointer

    get_checkpointer().delete_thread(thread_id)
    logfire.info(f"🗑️ Conversation memory cleared | thread={thread_id}")


def search_knowledge(query: str, top_k: int) -> list[dict[str, Any]]:
    """
    Knowledge-base search (hybrid dense + BM25 with RRF, or dense only, per
    RETRIEVAL_MODE) + FlashRank rerank, formatted with build_citations().
    No LLM call: cheap, fast, deterministic evidence lookup.

    Raises RetrievalError when Qdrant / embeddings are unreachable, so callers can
    tell an outage apart from "nothing matched" ([]).
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
