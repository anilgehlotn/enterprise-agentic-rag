# ============================================================
# CRITICAL: logfire MUST be configured before ALL other imports
# so that spans from all modules are captured from the start.
# ============================================================
import os
import tempfile
from pathlib import Path

import logfire
from dotenv import load_dotenv

load_dotenv()
# "if-token-present": with LOGFIRE_TOKEN set, traces go to Logfire exactly as before.
# Without it, telemetry is skipped instead of crashing at import ("not logged into
# Logfire"), so the container can start and serve /health without any secrets.
logfire.configure(token=os.getenv("LOGFIRE_TOKEN"), send_to_logfire="if-token-present")

# Now safe to import app modules - logfire is already active
import json
from collections.abc import Iterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, Response, UploadFile
from fastapi import Path as PathParam
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agents.checkpointer import open_checkpointer
from app.agents.graph import get_rag_agent, shutdown_rag_agent
from app.ingestion.chunking.config import resolve_chunking_config
from app.ingestion.processor import process_file
from app.rate_limit import limit_queries, limit_uploads, validate_rate_limits
from app.schemas import MAX_QUESTION_CHARS, MAX_THREAD_ID_CHARS, QueryResponse
from app.services import rag_service
from app.services.retrieval.schema import resolve_retrieval_mode


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    Startup: validate configuration, open the checkpointer, warm the guardrails.
    Shutdown (after the server stops accepting requests): close the checkpointer's
    SQLite file / Postgres pool, so no connection is left half-open on SIGTERM.
    """
    # Fail fast on a mistyped RETRIEVAL_MODE / RATE_LIMIT_* / CHECKPOINTER / CHUNKING_STRATEGY /
    # CHUNK_SIZE / CHUNK_OVERLAP instead of failing on the first query or upload.
    resolve_retrieval_mode()
    resolve_chunking_config()
    validate_rate_limits()
    # Raises ValueError on an invalid CHECKPOINTER value; for postgres it connects and
    # creates the checkpoint tables (setup()) here, once per process.
    open_checkpointer()
    try:
        # Warm-up only: rag_service.answer_question() also calls ensure_rails(), which is
        # idempotent, so every entry point (API and MCP server) is guarded either way.
        rag_service.ensure_rails()
        yield
    finally:
        shutdown_rag_agent()


# Initialize FastAPI
app = FastAPI(title="Enterprise Agentic RAG API", lifespan=lifespan)


class QueryRequest(BaseModel):
    q: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS, description="The user's question")
    thread_id: str | None = "default_user"


class UploadResult(BaseModel):
    filename: str
    chunks_indexed: int
    status: str = "indexed"


ALLOWED_DOCUMENT_EXTENSIONS = {".pdf", ".html", ".htm", ".txt", ".docx", ".pptx"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


@app.get("/")
def home():
    return {"message": "Enterprise LangGraph RAG API is live."}


@app.get("/health")
def health_check():
    """Lightweight readiness endpoint for local and container deployments."""
    return {"status": "ok", "service": "enterprise-agentic-rag"}


@app.get("/graph")
def get_graph_image():
    """
    Returns the Mermaid image of the agent's workflow.
    """
    try:
        png_bytes = get_rag_agent().get_graph().draw_mermaid_png()
        return Response(content=png_bytes, media_type="image/png")
    except Exception as e:
        return {"error": f"Could not generate graph image: {e}"}


@app.post(
    "/documents",
    response_model=list[UploadResult],
    status_code=201,
    dependencies=[Depends(limit_uploads)],
)
async def upload_documents(files: list[UploadFile] = File(...)):
    """Validate, index, and retain source metadata for user-provided documents."""
    if not files:
        raise HTTPException(status_code=400, detail="Attach at least one document.")

    uploaded = []
    for upload in files:
        filename = Path(upload.filename or "untitled").name
        extension = Path(filename).suffix.lower()
        if extension not in ALLOWED_DOCUMENT_EXTENSIONS:
            raise HTTPException(
                status_code=415,
                detail=f"{filename}: unsupported file type. Use PDF, HTML, TXT, DOCX, or PPTX.",
            )

        content = await upload.read()
        if not content:
            raise HTTPException(status_code=400, detail=f"{filename}: file is empty.")
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"{filename}: limit is 25 MB per file.")

        with tempfile.NamedTemporaryFile(suffix=extension, delete=False) as temporary_file:
            temporary_file.write(content)
            temporary_path = temporary_file.name

        try:
            result = process_file(temporary_path, filename, source_type="uploaded")
            if result and result.get("error") == "old_collection_schema":
                raise HTTPException(status_code=409, detail=f"{filename}: {result['detail']}")
            if not result or result.get("error"):
                raise HTTPException(
                    status_code=502,
                    detail=f"{filename}: processing or indexing failed. Check the backend logs for the provider error.",
                )
            uploaded.append(UploadResult(filename=filename, chunks_indexed=result["chunks_indexed"]))
        finally:
            Path(temporary_path).unlink(missing_ok=True)

    return uploaded


@app.post("/query", response_model=QueryResponse, dependencies=[Depends(limit_queries)])
def query(request: QueryRequest):
    """
    Executes the guardrails gate + LangGraph RAG flow with memory.
    The logic lives in app.services.rag_service so the MCP server shares it.
    """
    return rag_service.answer_question(request.q, request.thread_id)


def _sse(events: Iterator[tuple[str, dict]]) -> Iterator[str]:
    """Formats (event, data) pairs as Server-Sent Events: `event: <type>` + one-line JSON `data:`."""
    for event, data in events:
        yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/query/stream", dependencies=[Depends(limit_queries)])
def query_stream(request: QueryRequest):
    """
    Same request body and same guardrails + agent path as /query, streamed as
    Server-Sent Events: step* → token* → sources → done (or a final error event).
    The rate limit and request validation run before the stream starts, so they
    still return normal 429 / 422 responses.
    """
    return StreamingResponse(
        _sse(rag_service.stream_answer(request.q, request.thread_id)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Stop reverse proxies (nginx, some ingress controllers) from buffering
            # the stream into one late response.
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/threads/{thread_id}/reset")
def reset_thread(thread_id: str = PathParam(min_length=1, max_length=MAX_THREAD_ID_CHARS)):
    """Clears the conversation memory (all checkpoints) of one thread."""
    try:
        rag_service.reset_thread(thread_id)
    except Exception as exc:
        logfire.error(f"Could not reset thread {thread_id}: {exc}")
        raise HTTPException(status_code=503, detail="Conversation memory is unavailable; try again.") from None
    return {"thread_id": thread_id, "status": "cleared"}
