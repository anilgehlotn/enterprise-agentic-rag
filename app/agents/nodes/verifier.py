"""Self-RAG: check that the final answer is supported by the retrieved evidence.

The verdict is reported (state["grounded"] → API → UI badge); the answer itself
is not changed, so a flaky verifier can only mislabel, never corrupt, an answer.
"""

import logfire

from app.agents.llm import get_llm
from app.agents.routing import parse_groundedness
from app.agents.state import AgentState
from app.services.retrieval.citations import format_context


def build_verification_prompt(answer: str, documents: list[dict]) -> str:
    return f"""You are a strict fact-checker for a RAG system.

EVIDENCE (numbered sources):
{format_context(documents)}

ANSWER TO CHECK:
{answer}

Is every factual claim in the ANSWER supported by the EVIDENCE? General phrasing and
"the documents don't say" statements are fine; facts not present in the evidence are not.

Reply with ONLY a JSON object: {{"grounded": true}} or {{"grounded": false}}"""


def verify_node(state: AgentState):
    answer = state.get("final_answer") or ""
    documents = state.get("documents") or []

    with logfire.span("🔎 Groundedness Check", chunks=len(documents)):
        try:
            raw = get_llm("verifier").invoke(build_verification_prompt(answer, documents)).content
        except Exception as exc:
            logfire.error(f"Verifier call failed: {exc}")
            raw = None
        grounded = parse_groundedness(raw)
        logfire.info("Verifier verdict", raw=raw, grounded=grounded)

    step = {
        True: "Verification: answer supported by sources ✅",
        False: "Verification: answer not fully supported by sources ⚠️",
        None: "Verification: inconclusive",
    }[grounded]
    return {"grounded": grounded, "plan": state["plan"] + [step]}
