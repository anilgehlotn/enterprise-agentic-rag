# 🔑 Environment Variables & Configuration

The project uses a `.env` file for local development, loaded with `python-dotenv`. Provider keys and Qdrant settings are read in `app/config.py` (a plain `Settings` class over `os.getenv`); feature switches are read where they are used (`app/agents/graph.py`, `app/agents/checkpointer.py`, `app/rate_limit.py`, `app/services/retrieval/schema.py`). Copy `.env.example` to `.env` and fill in your values before running anything. `.env.example` lists every variable the code reads.

---

## 🧠 LLMs

| Variable | Description | Example |
| :--- | :--- | :--- |
| `GROQ_API_KEY` | **Required.** Used directly (not through Portkey) by NeMo Guardrails (`llama-3.1-8b-instant`), and by the RAGAS judge when `JUDGE_GROQ` is unset. Startup fails if it is empty (guardrails are built at startup; a dummy value is enough for `/health`). | `gsk_...` |
| `GROQ_FALLBACK_API_KEY` | Read into `settings` but **not used by the code**. The agent's Groq keys live in the Portkey dashboard as the `rag` (primary) and `brag` (fallback) integrations. Kept because the deploy workflow mounts it. | `gsk_...` |

---

## 🔀 LLM Gateway

| Variable | Description | Example |
| :--- | :--- | :--- |
| `PORTKEY_API_KEY` | API key for Portkey. Every agent LLM call (planner, grader, rewriter, responder, verifier) goes through the gateway, which routes to `@rag/llama-3.3-70b-versatile` with fallback to `@brag/llama-3.1-8b-instant` (slugs in `app/config.py`). | `pk-...` |

---

## 🌐 Gemini Embeddings

| Variable | Description | Example |
| :--- | :--- | :--- |
| `GEMINI_API_KEY` | Google Gemini API key used to generate 3072-dim embeddings via `gemini-embedding-2-preview` | `AIza...` |

---

## 🗄️ Vector Database

| Variable | Description | Example |
| :--- | :--- | :--- |
| `QDRANT_API_KEY` | Qdrant Cloud access token | `xyz...` |
| `QDRANT_CLUSTER_ENDPOINT` | Full URL of your Qdrant Cloud cluster | `https://your-cluster.cloud.qdrant.io:6333` |
| `QDRANT_COLLECTION` | Collection name (default `enterprise_rag`; empty also means default). Use a **new name when the vector schema changes**, e.g. `enterprise_rag_v2` for the hybrid dense + bm25 schema, so an older deployment keeps working on the old collection while you re-ingest into the new one. | `enterprise_rag_v2` |
| `RETRIEVAL_MODE` | `hybrid` (default): dense + BM25 prefetch fused with Qdrant RRF, then FlashRank. `dense`: dense-only search, then FlashRank. Any other value fails at API startup. Both modes use the same collection. | `hybrid` |
| `FASTEMBED_CACHE_PATH` | Optional. Where fastembed caches the `Qdrant/bm25` model (default: a temp directory). Read by fastembed itself; the Docker image sets it to `/opt/models`. | `/tmp/fastembed` |
| `FLASHRANK_CACHE_DIR` | Optional. Where FlashRank keeps its ONNX reranker model (default `/tmp/flashrank`). The Docker image sets it to `/opt/models`, where the model is pre-downloaded. | `/tmp/flashrank` |
| `PROCESSED_DATA_DIR` | Optional. Folder the MCP tool `list_indexed_documents` reads (default `<project root>/processed_data`). Ingestion always writes to `processed_data/` under the current working directory. | `./processed_data` |

---

## 🕵️ Observability

| Variable | Description | Example |
| :--- | :--- | :--- |
| `LOGFIRE_TOKEN` | Pydantic Logfire token — traces every API call, parsing step, and retrieval span | `logfire_...` |
| `LANGSMITH_API_KEY` | LangSmith token — records LangGraph node transitions, prompts, and token usage | `lsv2_...` |
| `LANGSMITH_PROJECT` | LangSmith project name to group traces (default `rag_scale_test`) | `enterprise_rag` |
| `LANGSMITH_TRACING` | Enable/disable LangSmith tracing (default `true`; `app/config.py` copies it to `LANGCHAIN_TRACING_V2`) | `true` |
| `LANGSMITH_ENDPOINT` | LangSmith API endpoint (default shown) | `https://api.smith.langchain.com` |

---

## 🧪 Evals

| Variable | Description | Example |
| :--- | :--- | :--- |
| `JUDGE_GROQ` | Separate Groq key used exclusively by the RAGAS eval pipeline as the judge LLM. Keeping it separate ensures eval runs cannot exhaust the production key. | `gsk_...` |

---

## 🧠 Agent

| Variable | Description | Example |
| :--- | :--- | :--- |
| `ENABLE_SELF_CORRECTION` | Default `true`: grader + one rewrite-and-retry + verifier (+2 LLM calls per technical question, more on a retry). `false`, `0`, `no` or `off` builds the original linear graph `planner → retriever → responder`. | `true` |

---

## 💾 Conversation Memory (LangGraph checkpointer)

The checkpointer stores the graph state per `thread_id` after every node, so a follow-up question on the same thread sees the earlier turns. Code: `app/agents/checkpointer.py`.

| Variable | Description | Example |
| :--- | :--- | :--- |
| `CHECKPOINTER` | `sqlite` (default): local file, survives API restarts on one machine. `memory`: in-process only, lost on restart (tests use this). `postgres`: shared database, required for persistent memory on Cloud Run. Any other value fails at API startup. | `sqlite` |
| `SQLITE_CHECKPOINT_PATH` | SQLite file for `CHECKPOINTER=sqlite`. The folder is created on startup and `/state/` is git- and docker-ignored. Unset: `<project root>/state/checkpoints.sqlite`, independent of the working directory (so an MCP server started by Claude Desktop uses the same file). A relative value you set is resolved from the working directory. Not `./data/`: on macOS/Windows that is the same folder as the `DATA/` corpus. | `/var/lib/rag/checkpoints.sqlite` |
| `DATABASE_URL` | Postgres connection string for `CHECKPOINTER=postgres`. Tables are created on startup (`PostgresSaver.setup()`, idempotent). **A secret**: on Cloud Run it comes from Secret Manager. | `postgresql://user:pass@ep-x.neon.tech/rag?sslmode=require` |

`POST /threads/{thread_id}/reset` deletes all checkpoints of one thread (the UI's **Clear History & Memory** button calls it).

---

## 🚦 Rate Limiting

Per-client-IP limits on the endpoints that spend LLM / embedding budget. Code: `app/rate_limit.py`. Exceeding a limit returns **429** with a `Retry-After` header. `/health` (and `/`, `/graph`, `/threads/.../reset`) are never limited. Limits are validated at startup.

| Variable | Description | Example |
| :--- | :--- | :--- |
| `RATE_LIMIT_QUERY` | Limit for `/query` and `/query/stream` together (one shared budget). Format `<count>/<second\|minute\|hour\|day>`. Default `20/minute`. | `20/minute` |
| `RATE_LIMIT_UPLOAD` | Limit for `/documents`. Default `5/minute`. | `5/minute` |
| `TRUST_PROXY_HEADERS` | `true`: identify clients by the **rightmost** `X-Forwarded-For` entry (the one our proxy wrote). `false` (default): use the TCP peer address and ignore the header, because any client can forge it to get a fresh budget per request. Set `true` only behind a proxy you control, e.g. Cloud Run. | `false` |

> **uvicorn has its own proxy-header handling.** By default uvicorn rewrites the client address from `X-Forwarded-For` when the TCP peer is in `FORWARDED_ALLOW_IPS` (default `127.0.0.1`), independently of `TRUST_PROXY_HEADERS`. The Docker image runs uvicorn with `--no-proxy-headers` so the app's setting is the only switch. Locally this only matters for requests from your own machine; add `--no-proxy-headers` to your `uvicorn` command to reproduce production behaviour.

> **Per instance.** The counters live in each process's memory. With Cloud Run `--max-instances=2`, one client can get at most 2× the configured limit, which is acceptable for this demo. Exact global limits across many instances need a shared store such as Redis.

---

## 🖥️ Backend

| Variable | Description | Example |
| :--- | :--- | :--- |
| `BACKEND_URL` | URL the Streamlit UI uses to reach the FastAPI backend (default `http://localhost:8000`, no trailing slash) | `http://localhost:8000` |
| `PORT` | Port uvicorn listens on inside the Docker image (default `8080`; Cloud Run sets it) | `8080` |

---

## 🔒 Security Best Practices
1.  **Never** commit your `.env` file to Git — it is in `.gitignore`.
2.  Use `.env.example` as the template when onboarding new developers.
3.  Keep `JUDGE_GROQ` on a separate key so eval workloads cannot rate-limit the live app.
