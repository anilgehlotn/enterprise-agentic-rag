"""Small dependency stubs keep pure unit tests independent of cloud services."""

import os
import sys
from contextlib import nullcontext
from types import SimpleNamespace

# Unit tests never trace to LangSmith. app.config (imported by the retrieval and ingestion
# tests) loads .env and would otherwise set LANGCHAIN_TRACING_V2=true for every later
# LangGraph test, sending runs over the network. load_dotenv() doesn't override these.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"


if "logfire" not in sys.modules:
    sys.modules["logfire"] = SimpleNamespace(
        configure=lambda *args, **kwargs: None,
        span=lambda *args, **kwargs: nullcontext(),
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )
