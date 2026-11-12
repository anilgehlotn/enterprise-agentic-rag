# Contributing

Thanks for your interest in improving this project. This guide covers setup, the checks every change must pass, the conventions used in this repository and the architecture rules that keep the entry points consistent.

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt  # what CI installs: no API keys, no heavy ML dependencies
```

`requirements-dev.txt` is enough to lint and run the whole test suite. To run the service against real backends, install `requirements.txt`, copy `.env.example` to `.env` and fill in the keys described in [DOCS/05_ENVIRONMENT_VARIABLES.md](DOCS/05_ENVIRONMENT_VARIABLES.md).

## Commands

```bash
ruff check .                                # lint (config in pyproject.toml, line length 120)
ruff format --check .                       # formatting
python -m pytest tests -q --cov=app         # tests; CI fails below COVERAGE_FLOOR in .github/workflows/ci.yml
uvicorn app.main:app --reload --port 8000   # API (needs a filled .env for real queries)
streamlit run ui/app.py                     # UI
python -m app.mcp_server                    # MCP server, stdio
python -m app.ingestion.processor DATA --wipe   # re-index the corpus (real keys, live Qdrant)
```

Run `ruff check`, `ruff format --check` and the tests before opening a pull request, and paste their real output into the pull request.

## Branches and commits

- Branch from `main`. Name the branch by type: `feature/<topic>`, `fix/<topic>` or `docs/<topic>` (for example `feature/hybrid-retrieval`).
- One issue per branch and pull request; the pull request closes its issue.
- Commit messages follow Conventional Commits: `type(scope): summary` in the imperative, for example `feat(retrieval): add BM25 sparse vectors`. Types in use: `feat`, `fix`, `docs`, `test`, `ci`, `chore`, `data`, `notebooks`.
- Reference the issue in the pull request description with `Closes #<number>`.

## Architecture rules

- **One service layer.** `/query`, `/query/stream` and the MCP tools go through `app/services/rag_service.py` (`_check_guardrails`, `_agent_inputs`, `_result_from_state`). Put new request logic there, never in an endpoint or tool, so the entry points can't drift apart.
- **Guardrails fail closed.** If they can't initialise or the check raises, the request returns an error; it must never reach the agent unguarded. The grader, by contrast, fails open (keeps all chunks).
- **Lazy clients, so tests need no keys.** Never create Qdrant, Portkey, Gemini, FlashRank, LLM or database clients at import time. Use the cached factories (`get_qdrant_client()`, `get_portkey_client()`, `get_llm()`, `get_rag_agent()`, `get_checkpointer()`) and import heavy packages inside functions. `tests/test_agent_graph.py::test_importing_agent_modules_creates_no_clients` enforces this for `app.agents`.
- **Pure routing.** Branch decisions and LLM-output parsing live in `app/agents/routing.py` with no LLM, network or LangGraph imports. Keep them pure and unit-tested; nodes call them and `graph.py` wires them in.
- **Stable `/query` contract.** The response shape is `question, answer, thought_process, status, sources, grounded` (`QueryResponse` in `app/schemas.py`); `sources` items match `build_citations()`. `evals/pipeline.py`, `evals/guardrails_eval.py`, the UI and the MCP tool parse it. Don't rename or remove fields; the `thought_process` strings are parsed by `evals/pipeline.detect_tool()`.
- **Streaming parity.** `/query/stream` must keep running the same guardrails + agent path as `/query`. Tokens are emitted only through `app/agents/streaming.token_writer()`, which is a no-op for `graph.invoke()`.
- **Per-request state reset.** Start every run with `new_request_state()`; only `messages` may carry over between turns on a `thread_id`.
- **Outage ≠ no results.** Retrieval failures raise `RetrievalError`; never return `[]` for an outage.
- **Logfire first in `app/main.py`.** `logfire.configure()` runs before the app imports (the reason for the `E402` ignore).

## Secrets and configuration

- No secrets in code, tests, docs, notebooks or workflow files. Keys come from `.env` locally and Secret Manager on Cloud Run. Never commit `.env`.
- Every new environment variable goes into `.env.example` (grouped, commented, safe default) and [DOCS/05_ENVIRONMENT_VARIABLES.md](DOCS/05_ENVIRONMENT_VARIABLES.md).
- Tests must not need API keys or network. Stub heavy modules via `sys.modules` and fake the nodes (see `tests/conftest.py` and the `api_app` fixture). `conftest.py` sets `CHECKPOINTER=memory` and resets the rate limiter and checkpointer around every test.

## Documentation

Keep `README.md`, `ARCHITECTURE.md` and `DOCS/` consistent with the code: node names, endpoints, environment variables and defaults. Don't publish performance numbers that weren't produced by a run in this repository; results that are not measured are reported as not measured.

## Proposing a change

1. Check the [open issues](https://github.com/anilgehlotn/enterprise-agentic-rag/issues) and the [roadmap](DOCS/ROADMAP.md). For anything larger than a small fix, open an issue first and describe the problem, the proposed approach and how it will be verified.
2. Create a branch, make the change, and add or update tests for it.
3. Run the three checks above.
4. Open a pull request against `main` with a summary, the list of changes, the test output and the related issue. CI (lint, tests with the coverage gate, Docker build and `/health` smoke test) must pass.
5. Report security problems privately, never in a public issue or pull request.
