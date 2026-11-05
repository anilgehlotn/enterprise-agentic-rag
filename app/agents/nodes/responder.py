import logfire
from app.agents.routing import is_conversational
from app.agents.state import AgentState, conversation_history, latest_user_question
from app.services.retrieval.citations import format_context


def no_evidence_answer(question: str) -> str:
    """
    Fixed reply when retrieval (after any rewrite) found no relevant documents.
    Deliberately not LLM-generated: the model must not fall back to general
    knowledge, and this path costs no tokens.
    """
    return (
        "I couldn't find this in the indexed documents, so I won't answer from general "
        f"knowledge. The current knowledge base doesn't appear to cover: \"{question}\".\n\n"
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

    # Imported lazily so importing this module needs no Portkey client or API key.
    from app.gateway import extract_cache_status, get_portkey_client

    with logfire.span("✍️ LLM Synthesis"):
        try:
            response = get_portkey_client().chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1
            )
            content = response.choices[0].message.content
            cache_status = extract_cache_status(response)
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
                "messages": [{"role": "assistant", "content": content}]
            }

        except Exception as e:
            logfire.error(f"LLM Generation failed: {e}")
            raise e
