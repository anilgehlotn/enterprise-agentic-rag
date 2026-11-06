"""Token streaming from inside graph nodes.

The responder calls Portkey's native client (for the cache-status header), not a
LangChain chat model, so LangGraph's "messages" stream mode can't see its tokens.
Instead the node pushes each chunk through LangGraph's custom stream writer, and
rag_service.stream_answer() reads them with stream_mode="custom".

Token streaming is opt-in per run: the caller sets STREAM_TOKENS_KEY in
config["configurable"]. /query (graph.invoke) never sets it, so the responder keeps
its original non-streaming LLM call there.
"""

from collections.abc import Callable

STREAM_TOKENS_KEY = "stream_tokens"

# Shape of a custom-stream item: {"type": TOKEN_ITEM, "text": "..."}.
TOKEN_ITEM = "token"


def token_writer() -> Callable[[str], None] | None:
    """
    Returns a function that emits one text chunk to the stream, or None when this run
    didn't ask for token streaming (graph.invoke, or the node called outside a graph,
    e.g. directly in a unit test, where get_config() raises RuntimeError).
    """
    from langgraph.config import get_config, get_stream_writer

    try:
        config = get_config()
    except RuntimeError:
        return None
    if not (config.get("configurable") or {}).get(STREAM_TOKENS_KEY):
        return None
    writer = get_stream_writer()
    return lambda text: writer({"type": TOKEN_ITEM, "text": text})
