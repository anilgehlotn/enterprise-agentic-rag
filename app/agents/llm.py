"""Lazy, cached LLM clients for agent nodes.

Nothing here runs at import time, so importing any app.agents module needs no
API keys, no Portkey/LangChain packages, and no network. The client is built on
the first call for each feature and reused afterwards.
"""

from functools import lru_cache


@lru_cache(maxsize=None)
def get_llm(feature: str):
    """Portkey-backed LangChain chat model, tagged with `feature` for gateway analytics."""
    from app.gateway import get_langchain_llm

    return get_langchain_llm(feature=feature)
