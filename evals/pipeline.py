"""
Phase 1 — Live Pipeline.
Calls the running FastAPI /query endpoint for each golden sample.
Captures: actual_response (truncated to 300 chars), actual_contexts (from sources),
and actual_tools_called (detected from thought_process).
"""


import time
import copy
import json
import os
import requests
import logfire

API_URL = "http://localhost:8000/query"
RESPONSE_TRUNCATE = 300
CONTEXT_LIMIT = 5          # number of retrieved source chunks kept per sample
DELAY_BETWEEN_CALLS = 10   # seconds — stays within Groq RPM on the main key
REQUEST_TIMEOUT = 120      # seconds — guardrails + LangGraph + Groq can take >60s




def detect_tool(thought_process: list) -> str:
    """
    Maps the thought_process list from /query response to a tool name.
    Planner sets:   'Intent: Technical' + 'Search Term: ...' → retrieve_documents
                    'Intent: Conversational/Memory'           → direct_answer
    Retriever sets: 'Retrieval attempt n/2: ...'              → retrieve_documents
                    (grader / rewriter / verifier steps only follow a retrieval)
    main.py sets:   'Intent: Guardrails Fired'                → guardrails
    Technical markers are checked before 'memory', because a rewritten search
    query such as 'kubernetes memory limits' would otherwise look conversational.
    """
    joined = " ".join(thought_process).lower()
    if "guardrails fired" in joined:
        return "guardrails"
    technical_markers = ("intent: technical", "search term:", "retrieval attempt", "context retrieved")
    if any(marker in joined for marker in technical_markers):
        return "retrieve_documents"
    if "conversational" in joined or "memory" in joined:
        return "direct_answer"
    return "unknown"


def extract_context_texts(sources: list, limit: int = CONTEXT_LIMIT) -> list[str]:
    """
    Converts /query `sources` (citation dicts) into the plain strings that
    evals.metrics expects in actual_contexts. Empty chunks are dropped.
    """
    texts: list[str] = []
    for source in sources:
        if isinstance(source, dict):
            text = source.get("content") or ""
        elif isinstance(source, str):
            text = source
        else:
            continue
        if text.strip():
            texts.append(text)
        if len(texts) >= limit:
            break
    return texts


def run_pipeline(golden_dataset: dict, progress_callback=None) -> dict:
    """
    Enriches each rag_sample in golden_dataset with live API results.
    Returns a deep copy with actual_response, actual_contexts, actual_tools_called filled.
    progress_callback(i, total, question, stage, response="") is called per step.
    """
    dataset = copy.deepcopy(golden_dataset)
    samples = dataset["rag_samples"]
    n = len(samples)

    with logfire.span("🚀 Eval Phase 1 — Live Pipeline", total_samples=n):
        for i, sample in enumerate(samples):
            question = sample["question"]

            if progress_callback:
                progress_callback(i, n, question, "calling")

            with logfire.span(
                f"📤 Live Query {i + 1}/{n}",
                question=question[:80],
                domain=sample.get("domain", ""),
            ):
                try:
                    resp = requests.post(
                        API_URL,
                        json={"q": question, "thread_id": f"eval_run_{i}"},
                        timeout=REQUEST_TIMEOUT,
                    )
                    resp.raise_for_status()
                    data = resp.json()

                    raw_answer = data.get("answer") or ""
                    thought_process = data.get("thought_process") or []
                    sources = data.get("sources") or []

                    sample["actual_response"] = raw_answer[:RESPONSE_TRUNCATE]
                    sample["actual_contexts"] = extract_context_texts(sources)
                    sample["actual_tools_called"] = [detect_tool(thought_process)]

                    logfire.info(
                        "✅ Response captured",
                        tool=sample["actual_tools_called"][0],
                        response_chars=len(raw_answer),
                        context_chunks=len(sources),
                    )

                except requests.exceptions.ConnectionError:
                    logfire.error("❌ Cannot reach FastAPI — is the app running on :8000?")
                    sample["actual_response"] = ""
                    sample["actual_contexts"] = sample.get("relevant_contexts", [])
                    sample["actual_tools_called"] = ["unknown"]

                except Exception as e:
                    logfire.error(f"❌ Query failed: {e}")
                    sample["actual_response"] = ""
                    sample["actual_contexts"] = sample.get("relevant_contexts", [])
                    sample["actual_tools_called"] = ["unknown"]

            if progress_callback:
                progress_callback(i, n, question, "done", sample["actual_response"])

            if i < n - 1:
                time.sleep(DELAY_BETWEEN_CALLS)

    return dataset


def save_results(dataset: dict, path: str) -> None:
    with open(path, "w") as f:
        json.dump(dataset, f, indent=2)
        
        
def load_golden_dataset() -> dict:
    golden_path = os.path.join(os.path.dirname(__file__), "golden_dataset.json")
    with open(golden_path) as f:
        return json.load(f)
