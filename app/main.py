# ============================================================
# CRITICAL: logfire MUST be configured before ALL other imports
# so that spans from all modules are captured from the start.
# ============================================================
import logfire
import os
import tempfile
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
logfire.configure(token=os.getenv("LOGFIRE_TOKEN"))

# Now safe to import app modules - logfire is already active
from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from app.agents.graph import get_rag_agent
from app.ingestion.processor import process_file
from app.schemas import MAX_QUESTION_CHARS, QueryResponse
from app.services import rag_service
from app.services.retrieval.schema import resolve_retrieval_mode

from pydantic import BaseModel, Field
from typing import Optional


# Initialize FastAPI
app = FastAPI(title="Enterprise Agentic RAG API")


@app.on_event("startup")
def startup_event():
    # Fail fast on a mistyped RETRIEVAL_MODE instead of failing on the first query.
    resolve_retrieval_mode()
    # Warm-up only: rag_service.answer_question() also calls ensure_rails(), which is
    # idempotent, so every entry point (API and MCP server) is guarded either way.
    rag_service.ensure_rails()

class QueryRequest(BaseModel):
    q: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS, description="The user's question")
    thread_id: Optional[str] = "default_user"


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


@app.post("/documents", response_model=list[UploadResult], status_code=201)
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
    
    
@app.post("/query", response_model=QueryResponse)
def query(request: QueryRequest):
    """
    Executes the guardrails gate + LangGraph RAG flow with memory.
    The logic lives in app.services.rag_service so the MCP server shares it.
    """
    return rag_service.answer_question(request.q, request.thread_id)
