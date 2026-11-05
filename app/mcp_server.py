"""Model Context Protocol (MCP) server for the Enterprise Agentic RAG system.

Exposes three READ-ONLY tools to MCP clients (Claude Desktop, Claude Code, VS Code):

    ask_knowledge_base      guardrails + self-correcting agent → cited, verified answer
    search_knowledge_base   hybrid (dense + BM25, RRF) + FlashRank evidence chunks, no LLM
    list_indexed_documents  indexed files with chunk counts

All logic lives in app.services.rag_service (shared with the FastAPI /query API).

Run:
    python -m app.mcp_server                                  # stdio (Claude Desktop / Claude Code)
    python -m app.mcp_server --transport http --port 8001     # streamable HTTP on 127.0.0.1

stdio rule: stdout carries the JSON-RPC protocol, so nothing else may write to it.
Logging (Python logging, logfire console, uvicorn) goes to stderr; the SDK also
points fd 1 at stderr while serving, so stray print() calls in dependencies can't
corrupt the stream.

Importing this module needs no API keys or network: the agent, guardrails, Qdrant
and LLM clients are created lazily on the first tool call.
"""

import argparse
import ipaddress
import logging
import os
import sys
from typing import Annotated, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from app.schemas import MAX_QUESTION_CHARS, SourceChunk
from app.services import rag_service
from app.services.retrieval.schema import RetrievalError

logger = logging.getLogger("app.mcp_server")

MIN_TOP_K, MAX_TOP_K = 1, 20
MAX_THREAD_ID_CHARS = 200
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8001

KNOWLEDGE_SCOPE = (
    "The knowledge base holds indexed enterprise technical documentation: Kubernetes "
    "(Jobs, CronJobs, Pods, autoscaling, work queues, architecture), Intel hardware "
    "(CPUs, SR-IOV, NICs, paging/EPT) and enterprise networking, plus any documents "
    "users have uploaded."
)

# Every tool only reads; none writes, uploads or deletes. The agent and search call
# external services (LLM gateway, vector DB), hence open_world_hint=True.
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
READ_ONLY_LOCAL = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


# ── Output models (become each tool's outputSchema) ───────────────────────────

class EvidenceSource(BaseModel):
    """A cited source. The number [n] in the answer refers to sources[n-1]."""

    source: str
    source_type: str = "unknown"
    retrieval_score: Optional[float] = None
    rerank_score: Optional[float] = None
    preview: str = ""


class AskResult(BaseModel):
    answer: str
    grounded: Optional[bool] = Field(
        default=None,
        description="True = verified against sources, False = not fully supported, null = not verified.",
    )
    status: str
    sources: list[EvidenceSource]
    thought_process: list[str]


class SearchResult(BaseModel):
    query: str
    results: list[SourceChunk]


class DocumentInfo(BaseModel):
    filename: str
    source_type: str
    chunk_count: int


class DocumentList(BaseModel):
    total_documents: int
    documents: list[DocumentInfo]


# ── Input validation (clear messages, never tracebacks) ───────────────────────

def _require_text(value: str, field: str, max_chars: int | None = None) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ToolError(f"`{field}` must not be empty.")
    if max_chars is not None and len(text) > max_chars:
        raise ToolError(f"`{field}` is too long ({len(text)} characters); the limit is {max_chars}.")
    return text


def _require_top_k(top_k: int) -> int:
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not MIN_TOP_K <= top_k <= MAX_TOP_K:
        raise ToolError(f"`top_k` must be an integer between {MIN_TOP_K} and {MAX_TOP_K}; got {top_k!r}.")
    return top_k


def _service_failure(action: str) -> ToolError:
    # Full details (with traceback) go to the server log on stderr, not to the client.
    logger.exception("MCP tool failed while %s", action)
    return ToolError(
        f"The knowledge base could not complete the request ({action} failed). "
        "Please try again shortly; the server log has the details."
    )


KNOWLEDGE_BASE_UNAVAILABLE_TOOL_ERROR = (
    "The knowledge base is temporarily unavailable (the vector database or embedding "
    "service could not be reached), so no search was performed. This is not an empty "
    "result. Please try again in a few minutes."
)


# ── Server and tools ─────────────────────────────────────────────────────────

mcp = MCPServer(
    name="enterprise-rag",
    title="Enterprise Agentic RAG",
    instructions=(
        f"{KNOWLEDGE_SCOPE} Use ask_knowledge_base for a cited, fact-checked answer; "
        "use search_knowledge_base to read raw evidence quickly without an LLM; use "
        "list_indexed_documents to see which files are available. All tools are read-only."
    ),
)


@mcp.tool(
    title="Ask the knowledge base",
    annotations=READ_ONLY,
    description=(
        f"Answer a question from the enterprise knowledge base. {KNOWLEDGE_SCOPE}\n\n"
        "Runs safety guardrails, then a self-correcting RAG agent: retrieves and reranks "
        "chunks, grades them for relevance, rewrites the query and retries once if nothing "
        "relevant is found, writes an answer with inline citations [1], [2] (sources[n-1]), "
        "and verifies it against the evidence (`grounded`). If the documents don't cover the "
        "question it says so instead of guessing. Takes several seconds and makes LLM calls.\n\n"
        "Prefer this when the user wants an explained answer. Prefer search_knowledge_base "
        "when you only need source passages to read or quote yourself. Off-topic or unsafe "
        "requests are declined by guardrails (status 'Blocked by guardrails.'). Use the same "
        "thread_id across calls to keep follow-up questions in one conversation."
    ),
)
def ask_knowledge_base(
    question: Annotated[
        str,
        Field(
            description="The question, in natural language.",
            json_schema_extra={"minLength": 1, "maxLength": MAX_QUESTION_CHARS},
        ),
    ],
    thread_id: Annotated[
        str,
        Field(
            description="Conversation id for follow-up memory. Reuse it for related questions.",
            json_schema_extra={"maxLength": MAX_THREAD_ID_CHARS},
        ),
    ] = "mcp-default",
) -> AskResult:
    question = _require_text(question, "question", MAX_QUESTION_CHARS)
    thread_id = _require_text(thread_id, "thread_id", MAX_THREAD_ID_CHARS)

    try:
        result = rag_service.answer_question(question, thread_id)
    except Exception:
        raise _service_failure("answering the question")

    if result.get("status") == "error":
        # answer_question already logged the cause; tell the client it failed.
        raise ToolError("The RAG pipeline hit an internal error while answering. Please try again shortly.")

    return AskResult(
        answer=result.get("answer") or "",
        grounded=result.get("grounded"),
        status=result.get("status") or "",
        sources=[EvidenceSource(**source) for source in result.get("sources") or []],
        thought_process=result.get("thought_process") or [],
    )


@mcp.tool(
    title="Search the knowledge base",
    annotations=READ_ONLY,
    description=(
        f"Semantic search over the enterprise knowledge base. {KNOWLEDGE_SCOPE}\n\n"
        "Returns the top_k most relevant raw text chunks (hybrid semantic + BM25 keyword "
        "search fused with RRF, then a cross-encoder rerank) with their source file and scores. No LLM is called: fast and cheap, but "
        "no answer is written and nothing is fact-checked.\n\n"
        "Prefer this to look up or quote exact passages, compare sources, or check whether "
        "a topic is covered at all. Prefer ask_knowledge_base for an explained, cited answer. "
        "An empty `results` list means nothing in the index matched."
    ),
)
def search_knowledge_base(
    query: Annotated[
        str,
        Field(
            description="Search text: keywords or a natural-language question.",
            json_schema_extra={"minLength": 1, "maxLength": MAX_QUESTION_CHARS},
        ),
    ],
    top_k: Annotated[
        int,
        Field(
            description=f"Number of chunks to return ({MIN_TOP_K}-{MAX_TOP_K}).",
            json_schema_extra={"minimum": MIN_TOP_K, "maximum": MAX_TOP_K},
        ),
    ] = 5,
) -> SearchResult:
    query = _require_text(query, "query", MAX_QUESTION_CHARS)
    top_k = _require_top_k(top_k)

    try:
        results = rag_service.search_knowledge(query, top_k)
    except RetrievalError:
        logger.exception("Knowledge base unavailable during search_knowledge_base")
        raise ToolError(KNOWLEDGE_BASE_UNAVAILABLE_TOOL_ERROR)
    except Exception:
        raise _service_failure("searching the knowledge base")

    return SearchResult(query=query, results=[SourceChunk(**chunk) for chunk in results])


@mcp.tool(
    title="List indexed documents",
    annotations=READ_ONLY_LOCAL,
    description=(
        "List the documents ingested into the knowledge base, with their source type "
        "('true' = curated docs, 'noisy' = distractor docs, 'uploaded' = user uploads) and "
        "number of indexed chunks. No LLM call, no network.\n\n"
        "Use this to discover which files exist before asking or searching, or to check "
        "whether a document has been indexed."
    ),
)
def list_indexed_documents() -> DocumentList:
    try:
        documents = rag_service.list_documents()
    except Exception:
        raise _service_failure("listing documents")

    return DocumentList(
        total_documents=len(documents),
        documents=[DocumentInfo(**document) for document in documents],
    )


# ── CLI ──────────────────────────────────────────────────────────────────────

def configure_logging(level: str = "INFO") -> None:
    """Send every log line to stderr. stdout is reserved for the stdio protocol."""
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    try:
        from dotenv import load_dotenv

        load_dotenv(rag_service.PROJECT_ROOT / ".env")
    except ImportError:  # pragma: no cover - python-dotenv is in requirements-prod.txt
        pass
    try:
        import logfire

        # Default console exporter writes to stdout, so point it at stderr explicitly.
        # if-token-present: without LOGFIRE_TOKEN, trace to the console (stderr) only
        # instead of failing with "not logged into Logfire".
        logfire.configure(
            token=os.getenv("LOGFIRE_TOKEN"),
            send_to_logfire="if-token-present",
            service_name="enterprise-rag-mcp",
            console=logfire.ConsoleOptions(output=sys.stderr),
        )
    except Exception as exc:  # pragma: no cover - tracing must never stop the server
        logger.warning("Logfire not configured: %s", exc)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.mcp_server",
        description="MCP server exposing the Enterprise Agentic RAG knowledge base as read-only tools.",
    )
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                        help="stdio for Claude Desktop / Claude Code (default); http for streamable HTTP.")
    parser.add_argument("--host", default=DEFAULT_HTTP_HOST,
                        help=f"HTTP bind address (default {DEFAULT_HTTP_HOST}, local machine only).")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT,
                        help=f"HTTP port (default {DEFAULT_HTTP_PORT}).")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    configure_logging(args.log_level)

    if args.transport == "stdio":
        logger.info("Starting MCP server 'enterprise-rag' on stdio")
        mcp.run("stdio")
        return

    if not _is_loopback(args.host):
        logger.warning(
            "Binding MCP HTTP server to %s exposes it beyond this machine. These tools have no "
            "authentication; put it behind an authenticating proxy.", args.host,
        )
    logger.info("Starting MCP server 'enterprise-rag' on http://%s:%d/mcp", args.host, args.port)
    mcp.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
