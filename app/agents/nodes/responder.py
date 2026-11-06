import logfire

from app.agents.routing import is_conversational
from app.agents.state import AgentState, conversation_history, latest_user_question
from app.agents.streaming import token_writer
from app.services.retrieval.citations import format_context


def no_evidence_answer(question: str) -> str:
    """
    Fixed reply when retrieval (after any rewrite) found no relevant documents.
    Deliberately not LLM-generated: the model must not fall back to general
    knowledge, and this path costs no tokens.
    """
    return (
        "I couldn't find this in the indexed documents, so I won't answer from general "
        f'knowledge. The current knowledge base doesn\'t appear to cover: "{question}".\n\n'
        "If you have a document that covers it, upload it with **Upload files** and ask again."
    )


def build_technical_prompt(documents: list[dict], history: str, question: str) -> str:
    return f"""
        You are a Senior Technical Architect.
        Answer the question using ONLY the numbered TECHNICAL CONTEXT below.

        Citation rules:
        - After each claim, cite the source(s) it comes from inline, like [1] or [2][3].
        - Only use the numbers shown in the context. Never invent a source number.
        - If the context does not contain the answer, say so instead of using outside knowledge.

        TECHNICAL CONTEXT:
        {format_context(documents)}

        CONVERSATION HISTORY:
        {history}

        USER QUESTION:
        "{question}"
        """


def _complete(prompt: str) -> tuple[str, str]:
    """One non-streaming LLM call (the /query path, unchanged). Returns (text, cache status)."""
    # Imported lazily so importing this module needs no Portkey client or API key.
    from app.gateway import extract_cache_status, get_portkey_client

    response = get_portkey_client().chat.completions.create(
        messages=[{"role": "user", "content": prompt}], temperature=0.1
    )
    return response.choices[0].message.content, extract_cache_status(response)


def _complete_streaming(prompt: str, emit) -> tuple[str, str]:
    """
    Streams the LLM answer, emitting each text chunk as it arrives (the /query/stream
    path). Returns the full text and the cache status.

    Cache detection: with stream=True Portkey returns the OpenAI Stream object, whose
    .response holds the HTTP response, so x-portkey-cache-status is read from the
    headers before the body is consumed. If the gateway omits that header for a
    streamed response, extract_cache_status() returns "MISS" and the "Cache: Hit"
    step is simply not shown; the answer itself is unaffected.
    """
    from app.gateway import extract_cache_status, get_portkey_client

    stream = get_portkey_client().chat.completions.create(
        messages=[{"role": "user", "content": prompt}], temperature=0.1, stream=True
    )
    cache_status = extract_cache_status(stream)
    parts = []
    for chunk in stream:
        if not chunk.choices:  # e.g. a trailing usage-only chunk
            continue
        text = chunk.choices[0].delta.content
        if text:
            parts.append(text)
            emit(text)
    return "".join(parts), cache_status


def generate_node(state: AgentState):
    """
    Synthesizes a response using both Documentation Context AND Conversation History.
    Uses the native Portkey client (not LangChain) so we can read the
    x-portkey-cache-status response header and surface Cache: Hit in the UI.
    """
    history_str = conversation_history(state)
    user_msg = latest_user_question(state)
    documents = state.get("documents") or []

    if is_conversational(state.get("current_query")):
        logfire.info("Generating conversational response using memory.")
        prompt = f"""
        You are a friendly and helpful Enterprise AI Assistant.
        Answer the user's latest message using the CONVERSATION HISTORY below.

        CONVERSATION HISTORY:
        {history_str}

        LATEST MESSAGE:
        "{user_msg}"
        """
    elif not documents:
        logfire.info("No relevant documents; returning the no-evidence answer.")
        content = no_evidence_answer(user_msg)
        return {
            "final_answer": content,
            "grounded": None,
            "status": "No relevant documents found.",
            "plan": state["plan"] + ["Answer: indexed documents don't cover this question"],
            "messages": [{"role": "assistant", "content": content}],
        }
    else:
        logfire.info("Generating technical RAG response.")
        prompt = build_technical_prompt(documents, history_str, user_msg)

    with logfire.span("✍️ LLM Synthesis"):
        try:
            emit = token_writer()
            if emit is None:
                content, cache_status = _complete(prompt)
            else:
                content, cache_status = _complete_streaming(prompt, emit)
            is_cache_hit = cache_status == "HIT"

            if is_cache_hit:
                logfire.info("⚡ Gateway Cache Hit — response served from Portkey cache.")
                plan_update = state["plan"] + ["Cache: Hit ⚡"]
                status = "Cache hit — instant response."
            else:
                logfire.info("✅ Response synthesised via LLM.")
                plan_update = state["plan"]
                status = "Response generated."

            return {
                "final_answer": content,
                "status": status,
                "plan": plan_update,
                "messages": [{"role": "assistant", "content": content}],
            }

        except Exception as e:
            logfire.error(f"LLM Generation failed: {e}")
            raise e
