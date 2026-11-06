# ⚠️ Known Gotchas & Architectural Decisions

This document tracks non-obvious platform quirks and explains why the architecture is designed the way it is.

---

## 1. Logfire Initialization Order (The "Poisoning" Bug)

**The Issue:**
When adding Logfire observability, you might see traces fail to appear in the dashboard, accompanied by the warning:
`No logs or spans will be created until logfire.configure() has been called.`

**The Reason:**
If any module in the application calls `logfire.info()` or `logfire.span()` *before* `logfire.configure()` has been executed, Logfire's internal state becomes "poisoned" for that process. It enters a silent, no-op mode and discards all subsequent traces, even if you call `.configure()` later.

If we were to import `settings` from `app.config` at the top of `app/main.py` to get the `LOGFIRE_TOKEN`, Python's import engine could inadvertently load nested modules (like our reranker or LLM clients) which might contain module-level Logfire calls, triggering this poisoning effect.

**The Solution:**
We bypass the `config.py` file entirely at the very top of `app/main.py`. We load the environment variables directly using `os.getenv()` and configure Logfire **before any other application imports occur**.

```python
# app/main.py (simplified)
import os

import logfire
from dotenv import load_dotenv

load_dotenv()
# "if-token-present": without LOGFIRE_TOKEN telemetry stays local instead of crashing at import.
logfire.configure(token=os.getenv("LOGFIRE_TOKEN"), send_to_logfire="if-token-present")

# Now safe to import app modules - logfire is already active
from app.agents.graph import get_rag_agent
from app.services import rag_service
```

`ruff` is configured to allow these late imports in `app/main.py` only (`E402` in `pyproject.toml`).
This guarantees that Logfire is fully awake and tracing before the rest of the application is loaded into memory.

---

## 2. The "Lazy Loading" Pattern (FlashRank & Gemini)

**The Issue:**
Loading heavy machine learning models (like FlashRank) or initializing large SDKs (like the Gemini client) at the very top of your files can cause two major problems:
1.  **FastAPI Startup Delays**: The server won't start responding to health checks until the models are loaded.
2.  **Logfire Poisoning**: If these SDKs make any internal calls before Logfire is configured, they can "poison" the process, causing your traces to disappear.

**The Solution:**
We implemented a **Lazy Loading** pattern across the entire application. We do not initialize the Gemini embedding client or the FlashRank ranker at the module level. Instead, we wrap them in "getter" functions that only trigger the first time they are actually needed.

```python
# app/services/retrieval/embedding.py (simplified)
_active_model = None

def _init():
    """Initialise the embedding model once per process, on first use."""
    global _active_model
    if _active_model is not None:
        return
    # Imported here so importing this module needs no Google SDK (CI, tests).
    from langchain_google_genai import GoogleGenerativeAIEmbeddings
    _active_model = GoogleGenerativeAIEmbeddings(model=GEMINI_MODEL, google_api_key=settings.GEMINI_API_KEY)
```

The same pattern is used everywhere a client is expensive or needs a key: `get_qdrant_client()`, `get_portkey_client()` and `get_llm()` are cached factories, FlashRank uses `_get_ranker()`, and the agent graph itself is built by `get_rag_agent()` on first use (there is no module-level `rag_agent`). Importing any `app.agents` module creates no client. A test enforces this in a fresh interpreter (`test_importing_agent_modules_creates_no_clients`), which is also why the test suite needs no API keys.

The one deliberate startup cost is the NeMo Guardrails warm-up in the FastAPI lifespan handler. Models that would otherwise be downloaded at startup (FlashRank, BM25, NeMo's MiniLM) are baked into the Docker image.
