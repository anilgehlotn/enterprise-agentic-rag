# CLAUDE.md

Guidance for AI coding assistants working in this repository.

## Project

Self-correcting RAG service: FastAPI API (`app/main.py`) + Streamlit UI (`ui/app.py`) + MCP server (`app/mcp_server.py`). A LangGraph agent (`app/agents/graph.py`: planner → retriever → grader → rewriter ↺ → responder → verifier) answers from a Qdrant collection using hybrid dense (Gemini) + BM25 retrieval with RRF and FlashRank reranking. LLM calls go through the Portkey gateway to Groq. NeMo Guardrails gate every question. Conversation memory is a LangGraph checkpointer (`CHECKPOINTER=memory|sqlite|postgres`). Python 3.11+.

## Commands

```bash
pip install -r requirements-dev.txt         # what CI installs (no keys, no heavy ML deps)
ruff check .                                # lint (config in pyproject.toml, line length 120)
ruff format --check .                       # formatting
python -m pytest tests -q --cov=app         # tests; CI fails below COVERAGE_FLOOR in .github/workflows/ci.yml
uvicorn app.main:app --reload --port 8000   # API (needs a filled .env for real queries)
streamlit run ui/app.py                     # UI
python -m app.mcp_server                    # MCP server, stdio
python -m app.ingestion.processor DATA --wipe   # re-index the corpus (real keys, live Qdrant)
```

Run all three checks before saying a change is done, and report their real output.

## Architecture rules

- **One service layer.** `/query`, `/query/stream` and the MCP tools go through `app/services/rag_service.py` (`_check_guardrails`, `_agent_inputs`, `_result_from_state`). Put new request logic there, never in an endpoint or tool, so the entry points can't drift apart.
- **Guardrails fail closed.** If they can't initialise or the check raises, the request returns an error; it must never reach the agent unguarded. The grader, by contrast, fails open (keeps all chunks).
- **Lazy clients, so tests need no keys.** Never create Qdrant, Portkey, Gemini, FlashRank, LLM or DB clients at import time. Use cached factories (`get_qdrant_client()`, `get_portkey_client()`, `get_llm()`, `get_rag_agent()`, `get_checkpointer()`) and import heavy packages inside functions. `tests/test_agent_graph.py::test_importing_agent_modules_creates_no_clients` enforces this for `app.agents`.
- **Pure routing.** Branch decisions and LLM-output parsing live in `app/agents/routing.py` with no LLM, network or LangGraph imports. Keep them pure and unit-tested; nodes call them and `graph.py` wires them in.
- **Stable `/query` contract.** The response shape is `question, answer, thought_process, status, sources, grounded` (`QueryResponse` in `app/schemas.py`); `sources` items match `build_citations()`. `evals/pipeline.py`, `evals/guardrails_eval.py`, the UI and the MCP tool parse it. Don't rename or remove fields; the `thought_process` strings are parsed by `evals/pipeline.detect_tool()`.
- **Streaming parity.** `/query/stream` must keep running the same guardrails + agent path as `/query`. Tokens are emitted only through `app/agents/streaming.token_writer()`, which is a no-op for `graph.invoke()`.
- **Per-request state reset.** Start every run with `new_request_state()`; only `messages` may carry over between turns on a `thread_id`.
- **Outage ≠ no results.** Retrieval failures raise `RetrievalError`; never return `[]` for an outage.
- **Logfire first in `app/main.py`.** `logfire.configure()` runs before the app imports (the reason for the `E402` ignore).

## Secrets and config

- No secrets in code, tests, docs, notebooks or workflow files. Keys come from `.env` locally and Secret Manager on Cloud Run.
- Every new environment variable goes into `.env.example` (grouped, commented, safe default) and `DOCS/05_ENVIRONMENT_VARIABLES.md`.
- Tests must not need API keys or network. Stub heavy modules via `sys.modules` and fake the nodes (see `tests/conftest.py` and the `api_app` fixture). `conftest.py` sets `CHECKPOINTER=memory` and resets the rate limiter and checkpointer around every test.

## Docs

Keep README.md, ARCHITECTURE.md and DOCS/ consistent with the code: node names, endpoints, env vars and defaults. Don't publish performance numbers that weren't produced by a run in this repo (for example, the retrieval benchmark has no results yet).

## Git

**Never run `git commit` or `git push`** (or anything else that creates commits or changes remotes, such as `git commit --amend`, `git rebase` or `gh pr merge`). The maintainer reviews and commits all changes. Leave the work as uncommitted changes in the working tree.
