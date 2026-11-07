# MCP Server

## What MCP is and why this project has one

The **Model Context Protocol (MCP)** is an open standard that lets AI applications (Claude Desktop, Claude Code, VS Code and others) discover and call external tools through one common JSON-RPC interface. A client starts or connects to an MCP server, asks it which tools it offers (`tools/list`), and its LLM decides when to call them (`tools/call`).

This project exposes its RAG system as an MCP server, so any MCP client can answer questions from the indexed Kubernetes, Intel and networking documentation with the same guardrails, self-correcting agent, citations and verification as the web UI, without custom integration code.

```text
Claude Desktop / Claude Code / VS Code
        │  MCP (stdio or streamable HTTP)
        ▼
app/mcp_server.py        input validation, friendly errors, tool descriptions
        │
        ▼
app/services/rag_service.py   ◀── also used by FastAPI POST /query
        │
        ├── guardrails (NeMo) → self-correcting LangGraph agent   (ask)
        ├── Qdrant search → FlashRank rerank                       (search)
        └── processed_data/<source_type>/*.json                    (list)
```

Built on the official Python SDK, **`mcp==2.3.0`** (`from mcp.server.mcpserver import MCPServer`; in 1.x this class was `FastMCP`).

## Tools

All three tools are **read-only** with respect to the knowledge base (`readOnlyHint: true`, `destructiveHint: false`). There is no upload, write or delete tool. The one thing a call stores is conversation memory: `ask_knowledge_base` records the question and answer under its `thread_id` in the checkpointer.

### `ask_knowledge_base`

Full pipeline: guardrails → planner → retrieve → rerank → grade → (rewrite + retry) → answer with inline citations → groundedness check. Uses LLM calls and takes several seconds.

| Argument | Type | Default | Rules |
|---|---|---|---|
| `question` | string | required | not blank, max 4000 characters (same as the API) |
| `thread_id` | string | `"mcp-default"` | not blank, max 200 characters; reuse it for follow-up questions |

Example input:

```json
{ "question": "How do I run a Kubernetes Job on a schedule?", "thread_id": "alice-session-1" }
```

Example output (`structuredContent`):

```json
{
  "answer": "Use a CronJob: it creates a Job on a repeating schedule written in cron syntax [1]. ...",
  "grounded": true,
  "status": "Response generated.",
  "sources": [
    {
      "source": "cronjobs.docx",
      "source_type": "true",
      "retrieval_score": 0.71,
      "rerank_score": 0.93,
      "preview": "A CronJob creates Jobs on a repeating schedule..."
    }
  ],
  "thought_process": [
    "Intent: Technical",
    "Search Term: kubernetes cronjob schedule",
    "Retrieval attempt 1/2: 5 chunks retrieved for 'kubernetes cronjob schedule'",
    "Grading (attempt 1): 3/5 chunks relevant",
    "Verification: answer supported by sources ✅"
  ]
}
```

- `[n]` in the answer refers to `sources[n-1]`.
- `grounded`: `true` = verified against sources, `false` = not fully supported, `null` = not verified (conversational, nothing found, or unclear).
- `query_id`: the answer's id in the query analytics (logged with source `mcp`, [14 Analytics](14_ANALYTICS.md)); `null` when `ANALYTICS_ENABLED=false`.
- Off-topic or jailbreak prompts return normally with `"status": "Blocked by guardrails."` and the guardrail's reply as `answer`.

### `search_knowledge_base`

Hybrid search (Qdrant dense + BM25, fused with RRF; dense only with `RETRIEVAL_MODE=dense`) + cross-encoder rerank (FlashRank). **No LLM call**: fast, cheap, nothing generated or fact-checked. Use it to read or quote raw passages, or to check whether a topic is covered at all. If Qdrant or the embedding service is down it returns a "temporarily unavailable" tool error, never an empty result list.

| Argument | Type | Default | Rules |
|---|---|---|---|
| `query` | string | required | not blank, max 4000 characters |
| `top_k` | integer | `5` | 1–20 |

```json
{ "query": "cronjob concurrencyPolicy", "top_k": 3 }
```

```json
{
  "query": "cronjob concurrencyPolicy",
  "results": [
    {
      "source": "cronjobs.docx",
      "source_type": "true",
      "content": "<full chunk text>",
      "preview": "<first 280 characters>",
      "retrieval_score": 0.68,
      "rerank_score": 0.91
    }
  ]
}
```

An empty `results` list means nothing in the index matched.

### `list_indexed_documents`

Lists ingested files with their source type (`true` = curated, `noisy` = distractors, `uploaded` = user uploads) and chunk count. No arguments, no LLM, no network.

```json
{
  "total_documents": 11,
  "documents": [
    { "filename": "cronjobs.docx", "source_type": "true", "chunk_count": 4 },
    { "filename": "README.txt", "source_type": "noisy", "chunk_count": 1 }
  ]
}
```

It reads `processed_data/<source_type>/*.json`, the files written during ingestion. The folder is resolved from the project root, not the current directory. Override it with `PROCESSED_DATA_DIR`. It shows what was ingested on this machine; if the Qdrant collection was wiped or filled elsewhere, the two can differ.

### Errors

Invalid input and service failures come back as an MCP tool error (`isError: true`) with a short message, never a traceback:

```text
Error executing tool search_knowledge_base: `top_k` must be an integer between 1 and 20; got 0.
Error executing tool ask_knowledge_base: `question` must not be empty.
Error executing tool search_knowledge_base: The knowledge base could not complete the request (searching the knowledge base failed). Please try again shortly; the server log has the details.
```

Full exception details are written to the server log (stderr) only.

## Running the server

```bash
pip install -r requirements.txt        # includes mcp==2.3.0

# stdio (default): the client starts the server as a subprocess
python -m app.mcp_server --transport stdio

# streamable HTTP: a long-running server at http://127.0.0.1:8001/mcp
python -m app.mcp_server --transport http --host 127.0.0.1 --port 8001
```

Run these from the project root, or set `PYTHONPATH` to it. The server loads `<project root>/.env` on start.

### Environment variables

| Variable | Needed for |
|---|---|
| `GROQ_API_KEY` | Guardrails (NeMo intent check) |
| `PORTKEY_API_KEY` | Planner, grader, rewriter, responder and verifier LLM calls |
| `GEMINI_API_KEY` | Query embeddings |
| `QDRANT_CLUSTER_ENDPOINT`, `QDRANT_API_KEY` | Vector search |
| `LOGFIRE_TOKEN` (optional) | Send traces to Logfire; without it traces print to stderr only |
| `ENABLE_SELF_CORRECTION` (optional) | `false` = linear agent (no grader/rewriter/verifier) |
| `PROCESSED_DATA_DIR` (optional) | Where `list_indexed_documents` looks |
| `QDRANT_COLLECTION`, `RETRIEVAL_MODE` (optional) | Same meaning as for the API (`enterprise_rag` default; `hybrid` default) |
| `CHECKPOINTER`, `SQLITE_CHECKPOINT_PATH`, `DATABASE_URL` (optional) | Where `ask_knowledge_base` stores conversation memory per `thread_id` (default: SQLite at `<project root>/state/checkpoints.sqlite`, whatever the working directory) |

If they're already in `.env`, you don't need to repeat them in the client config. `list_indexed_documents` works with no keys at all.

## Claude Desktop

Edit `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Use **absolute paths**: Claude Desktop starts the server from its own working directory, with the project's virtualenv Python.

macOS:

```json
{
  "mcpServers": {
    "enterprise-rag": {
      "command": "/Users/you/enterprise-agentic-rag/.venv/bin/python",
      "args": ["-m", "app.mcp_server", "--transport", "stdio"],
      "env": {
        "PYTHONPATH": "/Users/you/enterprise-agentic-rag",
        "GROQ_API_KEY": "gsk_...",
        "PORTKEY_API_KEY": "pk-...",
        "GEMINI_API_KEY": "AIza...",
        "QDRANT_CLUSTER_ENDPOINT": "https://your-cluster.cloud.qdrant.io:6333",
        "QDRANT_API_KEY": "..."
      }
    }
  }
}
```

Windows (backslashes must be escaped in JSON):

```json
{
  "mcpServers": {
    "enterprise-rag": {
      "command": "C:\\Users\\you\\enterprise-agentic-rag\\.venv\\Scripts\\python.exe",
      "args": ["-m", "app.mcp_server", "--transport", "stdio"],
      "env": {
        "PYTHONPATH": "C:\\Users\\you\\enterprise-agentic-rag",
        "GROQ_API_KEY": "gsk_...",
        "PORTKEY_API_KEY": "pk-...",
        "GEMINI_API_KEY": "AIza...",
        "QDRANT_CLUSTER_ENDPOINT": "https://your-cluster.cloud.qdrant.io:6333",
        "QDRANT_API_KEY": "..."
      }
    }
  }
}
```

Restart Claude Desktop fully. The three tools then appear under the tools (🔨) menu. If the server fails to start, Claude Desktop's MCP log shows the stderr output (macOS: `~/Library/Logs/Claude/mcp-server-enterprise-rag.log`; Windows: `%APPDATA%\Claude\logs\`).

## Claude Code

stdio (Claude Code starts the server):

```bash
claude mcp add enterprise-rag \
  --env PYTHONPATH=/Users/you/enterprise-agentic-rag \
  -- /Users/you/enterprise-agentic-rag/.venv/bin/python -m app.mcp_server --transport stdio
```

Or connect to a running HTTP server:

```bash
python -m app.mcp_server --transport http --port 8001      # in another terminal
claude mcp add --transport http enterprise-rag http://127.0.0.1:8001/mcp
```

Add `--scope project` to write the entry to `.mcp.json` for the whole team (keep keys out of it; rely on `.env`), or `--scope user` for all your projects. Check it with `claude mcp list` or `/mcp` inside Claude Code.

## Testing with MCP Inspector

MCP Inspector is the official browser UI for exercising a server by hand.

```bash
# stdio: Inspector starts the server for you
npx @modelcontextprotocol/inspector \
  /Users/you/enterprise-agentic-rag/.venv/bin/python -m app.mcp_server --transport stdio
```

Set `PYTHONPATH` to the project root in the Inspector's environment panel, or run the command from the project root. Then **Connect → Tools → List Tools**, pick a tool, fill in the arguments and **Run Tool**. Try `top_k: 0` or a blank question to see validation errors.

For HTTP, start `python -m app.mcp_server --transport http --port 8001`, run `npx @modelcontextprotocol/inspector`, choose **Streamable HTTP**, and use the URL `http://127.0.0.1:8001/mcp`.

## Security notes

- **Read-only tools.** Nothing exposed over MCP can upload, modify or delete documents or the vector index; the only write is `ask_knowledge_base`'s conversation memory for its `thread_id`. Uploading stays a deliberate action in the web UI/API. A client LLM that gets tricked by a prompt injection can still only read.
- **Guardrails apply to MCP too.** Both the API and MCP call `rag_service.answer_question()`, which initialises NeMo Guardrails lazily (once per process) and gates every question. If guardrails can't start, the request fails closed (error), and never runs unguarded.
- **Localhost by default.** The HTTP transport binds to `127.0.0.1`, and the SDK's DNS-rebinding protection is active for localhost. The tools have **no authentication**: don't bind to `0.0.0.0` or a public interface unless an authenticating reverse proxy sits in front. The server logs a warning if you try.
- **Input limits.** Questions and queries are capped at 4000 characters and `top_k` at 20, which bounds cost per call.
- **No leaking internals.** Errors returned to the client are generic; stack traces, hostnames and keys stay in the server log.
- **stdout is the protocol in stdio mode.** All logging (Python `logging`, Logfire console, uvicorn) goes to stderr, and the SDK points file descriptor 1 at stderr while serving, so a stray `print()` in a dependency can't corrupt the JSON-RPC stream.
- **Shared memory per `thread_id`.** Conversation memory is keyed by `thread_id` and stored by the checkpointer selected with `CHECKPOINTER` (default: the SQLite file `./state/checkpoints.sqlite`, shared with the API when both run from the project root). Use a distinct `thread_id` per user or session if several people share one HTTP server.

## Tests

`tests/test_mcp_server.py` drives the server through the SDK's real client (`mcp.Client`) with the service layer mocked: tool listing and schemas, each tool's output, validation errors, friendly service errors, and a real stdio subprocess handshake that also checks a stray `print()` doesn't break the stream. No API keys or network needed:

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```
