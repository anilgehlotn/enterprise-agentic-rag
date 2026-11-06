# 🔑 Environment Variables & Configuration

The project uses a `.env` file for local development. All configuration is managed via **Pydantic Settings** in `app/config.py` for strict type safety. Copy `.env.example` to `.env` and fill in your values before running anything.

---

## 🧠 LLMs

| Variable | Description | Example |
| :--- | :--- | :--- |
| `GROQ_API_KEY` | Primary key for Groq LLM calls (Planner + Responder nodes) | `gsk_...` |
| `GROQ_FALLBACK_API_KEY` | Second Groq key used by Portkey as the fallback target; can be the same as primary | `gsk_...` |

---

## 🔀 LLM Gateway

| Variable | Description | Example |
| :--- | :--- | :--- |
| `PORTKEY_API_KEY` | API key for Portkey — enables routing, fallback, caching, and observability across all LLM calls | `pk-...` |

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
| `FASTEMBED_CACHE_PATH` | Optional. Where fastembed caches the `Qdrant/bm25` model (default: a temp directory) | `/tmp/fastembed` |

---

## 🕵️ Observability

| Variable | Description | Example |
| :--- | :--- | :--- |
| `LOGFIRE_TOKEN` | Pydantic Logfire token — traces every API call, parsing step, and retrieval span | `logfire_...` |
| `LANGSMITH_API_KEY` | LangSmith token — records LangGraph node transitions, prompts, and token usage | `lsv2_...` |
| `LANGSMITH_PROJECT` | LangSmith project name to group traces | `enterprise_rag` |
| `LANGSMITH_TRACING` | Enable/disable LangSmith tracing | `true` |
| `LANGSMITH_ENDPOINT` | LangSmith API endpoint | `https://api.smith.langchain.com` |

---

## 🧪 Evals

| Variable | Description | Example |
| :--- | :--- | :--- |
| `JUDGE_GROQ` | Separate Groq key used exclusively by the RAGAS eval pipeline as the judge LLM. Keeping it separate ensures eval runs cannot exhaust the production key. | `gsk_...` |

---

## 💾 Conversation Memory (LangGraph checkpointer)

The checkpointer stores the graph state per `thread_id` after every node, so a follow-up question on the same thread sees the earlier turns. Code: `app/agents/checkpointer.py`.

| Variable | Description | Example |
| :--- | :--- | :--- |
| `CHECKPOINTER` | `sqlite` (default): local file, survives API restarts on one machine. `memory`: in-process only, lost on restart (tests use this). `postgres`: shared database, required for persistent memory on Cloud Run. Any other value fails at API startup. | `sqlite` |
| `SQLITE_CHECKPOINT_PATH` | SQLite file for `CHECKPOINTER=sqlite`. The folder is created on startup and `/state/` is git- and docker-ignored. Default `./state/checkpoints.sqlite` (not `./data/`: on macOS/Windows that is the same folder as the `DATA/` corpus). | `./state/checkpoints.sqlite` |
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
| `BACKEND_URL` | URL the Streamlit UI uses to reach the FastAPI backend | `http://localhost:8000` |

---

## 🔒 Security Best Practices
1.  **Never** commit your `.env` file to Git — it is in `.gitignore`.
2.  Use `.env.example` as the template when onboarding new developers.
3.  Keep `JUDGE_GROQ` on a separate key so eval workloads cannot rate-limit the live app.
