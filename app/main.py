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
from app.agents.graph import rag_agent
from app.guardrails import initialize_rails, guard
from app.ingestion.processor import process_file
from app.services.retrieval.citations import build_citations

from pydantic import BaseModel, Field
from typing import Optional


# Initialize FastAPI
app = FastAPI(title="Enterprise Agentic RAG API")


@app.on_event("startup")
def startup_event():
    initialize_rails()

class QueryRequest(BaseModel):
    q: str = Field(min_length=1, max_length=4_000, description="The user's question")
    thread_id: Optional[str] = "default_user"


class SourceChunk(BaseModel):
    source: str
    source_type: str = "unknown"
    content: str
    rerank_score: Optional[float] = None
    score: Optional[float] = None


class QueryResponse(BaseModel):
    question: str
    answer: str
    thought_process: list[str]
    status: str
    sources: list[SourceChunk]


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
        png_bytes = rag_agent.get_graph().draw_mermaid_png()
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
            if not result:
                raise HTTPException(status_code=422, detail=f"{filename}: could not extract usable text.")
            uploaded.append(UploadResult(filename=filename, chunks_indexed=result["chunks_indexed"]))
        finally:
            Path(temporary_path).unlink(missing_ok=True)

    return uploaded
    
    
@app.post("/query", response_model=QueryResponse)
def query(request: QueryRequest):
    """
    Executes the LangGraph RAG flow with memory using a POST request.
    """
    q = request.q
    thread_id = request.thread_id

    initial_state = {
        "messages": [{"role": "user", "content": q}],
        "current_query": q,
        "documents": [],
        "plan": ["Start"],
        "status": "Initializing Graph..."
    }
    
    # Configuration for Memory (Thread ID)
    config = {"configurable": {"thread_id": thread_id}}
    
    try:
        # Gate 1: NeMo Guardrails — blocks off-topic, jailbreaks, and handles dialog
        rail_fired, rail_response = guard(q)
        if rail_fired:
            logfire.info(f"🛡️ Request blocked by guardrails | thread={thread_id}")
            return {
                "question": q,
                "answer": rail_response,
                "thought_process": ["Intent: Guardrails Fired", "Retrieval: Skipped"],
                "status": "Blocked by guardrails.",
                "sources": []
            }

        # Gate 2: LangGraph RAG pipeline
        # Run the graph synchronously to preserve Logfire context variables
        final_output = rag_agent.invoke(initial_state, config=config)
        
        return {
            "question": q,
            "answer": final_output.get("final_answer"),
            "thought_process": final_output.get("plan"),
            "status": final_output.get("status"),
            "sources": build_citations(final_output.get("documents", []))
        }
    except Exception as e:
        logfire.error(f"❌ Backend Execution Failed: {e}")
        return {
            "question": q,
            "answer": "I apologize, but I encountered an internal error while processing your request. Please try again later.",
            "thought_process": ["Error encountered during execution."],
            "status": "error",
            "sources": []
        }
