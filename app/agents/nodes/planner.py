import logfire

from app.agents.llm import get_llm
from app.agents.routing import CONVERSATIONAL, normalize_planner_decision
from app.agents.state import AgentState, conversation_history


def planner_node(state: AgentState):
    """
    The Planner determines if a search is needed based on the ENTIRE conversation.
    """
    history = conversation_history(state)
    user_message = state["messages"][-1]["content"] if state["messages"] else ""

    prompt = f"""
    You are an intelligent Assistant Planner.
    Analyze the conversation history and the latest user message.

    CONVERSATION HISTORY:
    {history}

    LATEST MESSAGE:
    "{user_message}"

    Task:
    1. If the latest message is a greeting (hi, hello) or a question that can be answered using ONLY the conversation history above (e.g., "what is my name"), respond with 'CONVERSATIONAL'.
    2. If it is a technical question about Kubernetes, Intel, or Networking that requires fresh documentation, output a refined search query.

    Output ONLY 'CONVERSATIONAL' or the search query.
    """  # noqa: E501 -- LLM prompt text; wrapping it would change the prompt

    with logfire.span("🧠 Planner Decision"):
        # Portkey-backed LLM (fallback + cache + retry), created lazily on first use.
        raw_decision = get_llm("planner").invoke(prompt).content
        decision = normalize_planner_decision(raw_decision)
        logfire.info(f"Intent identified: {decision}", raw_decision=raw_decision)

    if decision == CONVERSATIONAL:
        return {
            "current_query": CONVERSATIONAL,
            "status": "Handling conversationally (using memory)...",
            "plan": ["Intent: Conversational/Memory", "Retrieval: Skipped"],
        }

    return {
        "current_query": decision,
        "status": f"Technical research needed. Searching for: {decision}",
        "plan": ["Intent: Technical", f"Search Term: {decision}"],
    }
