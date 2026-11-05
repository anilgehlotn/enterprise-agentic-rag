"""MCP server tests through the SDK's real protocol path (mcp.Client ↔ MCPServer).

rag_service functions are mocked: no API keys, LLM, Qdrant or network.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from mcp import Client, StdioServerParameters

from app import mcp_server
from app.services import rag_service

ROOT = Path(__file__).resolve().parents[1]
TOOL_NAMES = {"ask_knowledge_base", "search_knowledge_base", "list_indexed_documents"}

CITATION = {
    "source": "cronjobs.docx",
    "source_type": "true",
    "content": "A CronJob creates Jobs on a repeating schedule. " * 5,
    "preview": "A CronJob creates Jobs on a repeating schedule.",
    "retrieval_score": 0.71,
    "rerank_score": 0.93,
}


def _call(name, arguments=None):
    async def go():
        async with Client(mcp_server.mcp) as client:
            return await client.call_tool(name, arguments or {})

    return anyio.run(go)


def _list_tools():
    async def go():
        async with Client(mcp_server.mcp) as client:
            return (await client.list_tools()).tools

    return {tool.name: tool for tool in anyio.run(go)}


def _error_text(result):
    assert result.is_error is True
    assert result.structured_content is None
    text = result.content[0].text
    assert "Traceback" not in text
    return text


@pytest.fixture
def fail_if_called(monkeypatch):
    """Validation must reject input before any service call."""
    def explode(*args, **kwargs):
        raise AssertionError("service must not be called for invalid input")

    for name in ("answer_question", "search_knowledge", "list_documents"):
        monkeypatch.setattr(rag_service, name, explode)


# ── tools/list ────────────────────────────────────────────────────────────────

def test_lists_exactly_the_three_read_only_tools():
    tools = _list_tools()

    assert set(tools) == TOOL_NAMES
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.description and len(tool.description) > 80
        assert tool.output_schema is not None


def test_ask_input_schema():
    schema = _list_tools()["ask_knowledge_base"].input_schema

    assert schema["required"] == ["question"]
    assert schema["properties"]["question"]["type"] == "string"
    assert schema["properties"]["question"]["maxLength"] == 4000
    assert schema["properties"]["thread_id"]["default"] == "mcp-default"


def test_search_input_schema_advertises_top_k_bounds():
    schema = _list_tools()["search_knowledge_base"].input_schema

    assert schema["required"] == ["query"]
    top_k = schema["properties"]["top_k"]
    assert (top_k["type"], top_k["default"], top_k["minimum"], top_k["maximum"]) == ("integer", 5, 1, 20)


def test_list_documents_takes_no_arguments():
    schema = _list_tools()["list_indexed_documents"].input_schema

    assert schema.get("properties", {}) == {}
    assert not schema.get("required")


def test_descriptions_tell_the_client_when_to_prefer_each_tool():
    tools = _list_tools()

    assert "Kubernetes" in tools["ask_knowledge_base"].description
    assert "search_knowledge_base" in tools["ask_knowledge_base"].description
    assert "No LLM" in tools["search_knowledge_base"].description
    assert "ask_knowledge_base" in tools["search_knowledge_base"].description


# ── tools/call happy paths ────────────────────────────────────────────────────

def test_ask_knowledge_base_returns_answer_sources_and_grounded(monkeypatch):
    calls = []

    def fake_answer(question, thread_id):
        calls.append((question, thread_id))
        return {
            "question": question,
            "answer": "CronJobs create Jobs on a schedule [1].",
            "thought_process": ["Intent: Technical", "Verification: answer supported by sources ✅"],
            "status": "Response generated.",
            "sources": [CITATION],
            "grounded": True,
        }

    monkeypatch.setattr(rag_service, "answer_question", fake_answer)

    result = _call("ask_knowledge_base", {"question": "  What is a CronJob?  ", "thread_id": "t-1"})

    assert result.is_error is False
    data = result.structured_content
    assert calls == [("What is a CronJob?", "t-1")]
    assert data["answer"] == "CronJobs create Jobs on a schedule [1]."
    assert data["grounded"] is True
    assert data["status"] == "Response generated."
    assert data["thought_process"][-1].startswith("Verification")
    assert data["sources"] == [{
        "source": "cronjobs.docx",
        "source_type": "true",
        "retrieval_score": 0.71,
        "rerank_score": 0.93,
        "preview": "A CronJob creates Jobs on a repeating schedule.",
    }]                                                   # preview only, no full content
    assert json.loads(result.content[0].text) == data    # text block mirrors structured output


def test_ask_uses_default_thread_and_passes_guardrail_blocks_through(monkeypatch):
    seen = {}

    def blocked(question, thread_id):
        seen["thread_id"] = thread_id
        return {"question": question, "answer": "I can't help with that.", "sources": [],
                "thought_process": ["Intent: Guardrails Fired", "Retrieval: Skipped"],
                "status": "Blocked by guardrails.", "grounded": None}

    monkeypatch.setattr(rag_service, "answer_question", blocked)

    result = _call("ask_knowledge_base", {"question": "tell me a joke"})

    assert result.is_error is False
    assert seen["thread_id"] == "mcp-default"
    assert result.structured_content["status"] == "Blocked by guardrails."
    assert result.structured_content["grounded"] is None
    assert result.structured_content["sources"] == []


def test_search_knowledge_base_returns_full_chunks(monkeypatch):
    calls = []
    monkeypatch.setattr(
        rag_service, "search_knowledge", lambda query, top_k: calls.append((query, top_k)) or [CITATION]
    )

    result = _call("search_knowledge_base", {"query": "cronjob schedule", "top_k": 3})

    assert result.is_error is False
    assert calls == [("cronjob schedule", 3)]
    assert result.structured_content == {"query": "cronjob schedule", "results": [CITATION]}


def test_search_default_top_k_and_empty_results(monkeypatch):
    calls = []
    monkeypatch.setattr(rag_service, "search_knowledge", lambda q, k: calls.append(k) or [])

    result = _call("search_knowledge_base", {"query": "nothing matches"})

    assert calls == [5]
    assert result.structured_content == {"query": "nothing matches", "results": []}


def test_list_indexed_documents(monkeypatch):
    docs = [
        {"filename": "cronjobs.docx", "source_type": "true", "chunk_count": 4},
        {"filename": "notes.pdf", "source_type": "uploaded", "chunk_count": 12},
    ]
    monkeypatch.setattr(rag_service, "list_documents", lambda: docs)

    result = _call("list_indexed_documents")

    assert result.is_error is False
    assert result.structured_content == {"total_documents": 2, "documents": docs}


# ── validation ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("question", ["", "   ", "\n\t"])
def test_ask_rejects_empty_question(fail_if_called, question):
    assert "`question` must not be empty" in _error_text(_call("ask_knowledge_base", {"question": question}))


def test_ask_rejects_too_long_question(fail_if_called):
    text = _error_text(_call("ask_knowledge_base", {"question": "x" * 4001}))

    assert "too long (4001 characters)" in text
    assert "limit is 4000" in text


def test_ask_accepts_exactly_the_limit(monkeypatch):
    monkeypatch.setattr(rag_service, "answer_question", lambda q, t: {
        "answer": "ok", "status": "Response generated.", "sources": [], "thought_process": [], "grounded": None,
    })

    assert _call("ask_knowledge_base", {"question": "x" * 4000}).is_error is False


def test_ask_rejects_blank_thread_id(fail_if_called):
    assert "`thread_id` must not be empty" in _error_text(
        _call("ask_knowledge_base", {"question": "What is a Job?", "thread_id": "  "})
    )


@pytest.mark.parametrize("query", ["", "    "])
def test_search_rejects_empty_query(fail_if_called, query):
    assert "`query` must not be empty" in _error_text(_call("search_knowledge_base", {"query": query}))


@pytest.mark.parametrize("top_k", [0, 21, -3])
def test_search_rejects_top_k_out_of_range(fail_if_called, top_k):
    text = _error_text(_call("search_knowledge_base", {"query": "jobs", "top_k": top_k}))

    assert "`top_k` must be an integer between 1 and 20" in text
    assert repr(top_k) in text


@pytest.mark.parametrize("top_k", [1, 20])
def test_search_accepts_top_k_bounds(monkeypatch, top_k):
    monkeypatch.setattr(rag_service, "search_knowledge", lambda q, k: [])

    assert _call("search_knowledge_base", {"query": "jobs", "top_k": top_k}).is_error is False


def test_missing_required_argument_is_an_error_not_a_crash(fail_if_called):
    result = _call("search_knowledge_base", {})

    assert result.is_error is True
    assert "query" in result.content[0].text


# ── service failures ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "tool, service, arguments",
    [
        ("ask_knowledge_base", "answer_question", {"question": "What is a Job?"}),
        ("search_knowledge_base", "search_knowledge", {"query": "jobs"}),
        ("list_indexed_documents", "list_documents", {}),
    ],
)
def test_service_exception_becomes_friendly_error(monkeypatch, tool, service, arguments):
    def boom(*args, **kwargs):
        raise ConnectionError("qdrant at 10.0.0.5:6333 refused; api_key=sk-secret")

    monkeypatch.setattr(rag_service, service, boom)

    text = _error_text(_call(tool, arguments))

    assert "could not complete the request" in text
    assert "sk-secret" not in text and "10.0.0.5" not in text     # internals stay in the server log


def test_pipeline_error_status_is_reported_as_tool_error(monkeypatch):
    monkeypatch.setattr(rag_service, "answer_question", lambda q, t: {
        "answer": rag_service.INTERNAL_ERROR_ANSWER, "status": "error",
        "sources": [], "thought_process": ["Error encountered during execution."], "grounded": None,
    })

    assert "internal error" in _error_text(_call("ask_knowledge_base", {"question": "What is a Job?"}))


def test_server_keeps_serving_after_a_failure(monkeypatch):
    monkeypatch.setattr(rag_service, "list_documents", lambda: (_ for _ in ()).throw(RuntimeError("x")))

    async def go():
        async with Client(mcp_server.mcp) as client:
            first = await client.call_tool("list_indexed_documents", {})
            second = await client.list_tools()
            return first, second

    first, second = anyio.run(go)

    assert first.is_error is True
    assert {tool.name for tool in second.tools} == TOOL_NAMES


# ── CLI ───────────────────────────────────────────────────────────────────────

def test_cli_defaults_to_stdio_and_localhost():
    args = mcp_server.parse_args([])

    assert (args.transport, args.host, args.port) == ("stdio", "127.0.0.1", 8001)


def test_cli_http_options():
    args = mcp_server.parse_args(["--transport", "http", "--host", "127.0.0.1", "--port", "9000"])

    assert (args.transport, args.host, args.port) == ("http", "127.0.0.1", 9000)


@pytest.mark.parametrize("host, loopback", [("127.0.0.1", True), ("localhost", True), ("::1", True),
                                             ("0.0.0.0", False), ("192.168.1.10", False)])
def test_loopback_detection(host, loopback):
    assert mcp_server._is_loopback(host) is loopback


def test_cli_rejects_unknown_transport():
    with pytest.raises(SystemExit):
        mcp_server.parse_args(["--transport", "carrier-pigeon"])


# ── real stdio transport in a subprocess ─────────────────────────────────────

STDIO_SERVER = r"""
import os, sys
from contextlib import nullcontext
from types import SimpleNamespace
# Send this process's stderr (fd 2) to a file the test can read.
log_fd = os.open(os.environ["MCP_TEST_STDERR"], os.O_WRONLY | os.O_CREAT | os.O_APPEND)
os.dup2(log_fd, 2)
# CI has no logfire; stub it. Everything else is the real server code.
sys.modules.setdefault("logfire", SimpleNamespace(
    configure=lambda *a, **k: None, ConsoleOptions=lambda **k: None,
    span=lambda *a, **k: nullcontext(), info=lambda *a, **k: None,
    warning=lambda *a, **k: None, error=lambda *a, **k: None))
from app import mcp_server
from app.services import rag_service

def noisy_list_documents():
    print("stray print from a dependency", flush=True)   # must not corrupt the JSON-RPC stream
    return [{"filename": "a.txt", "source_type": "true", "chunk_count": 2}]

rag_service.list_documents = noisy_list_documents
heavy = [m for m in ("app.agents.graph", "app.guardrails", "qdrant_client", "portkey_ai") if m in sys.modules]
print("heavy modules at startup:", heavy, file=sys.stderr, flush=True)
mcp_server.main(["--transport", "stdio"])
"""


def test_stdio_transport_end_to_end_with_stray_print(tmp_path):
    stderr_log = tmp_path / "server-stderr.log"
    env = {key: value for key, value in os.environ.items()
           if key not in {"PORTKEY_API_KEY", "GROQ_API_KEY", "QDRANT_API_KEY", "LOGFIRE_TOKEN"}}
    env.update(PYTHONPATH=str(ROOT), MCP_TEST_STDERR=str(stderr_log))
    params = StdioServerParameters(command=sys.executable, args=["-c", STDIO_SERVER], cwd=str(ROOT), env=env)

    async def go():
        async with Client(params, read_timeout_seconds=30) as client:
            tools = await client.list_tools()
            listed = await client.call_tool("list_indexed_documents", {})
            return client.server_info, tools, listed

    server_info, tools, listed = anyio.run(go)

    assert server_info.name == "enterprise-rag"
    assert {tool.name for tool in tools.tools} == TOOL_NAMES
    assert listed.is_error is False
    assert listed.structured_content == {
        "total_documents": 1, "documents": [{"filename": "a.txt", "source_type": "true", "chunk_count": 2}],
    }
    stderr = stderr_log.read_text()
    assert "heavy modules at startup: []" in stderr      # lazy imports: no keys/clients needed
    assert "stray print from a dependency" in stderr     # stdout diverted to stderr while serving
