import json
import os
import uuid

import logfire
import requests
import streamlit as st
from dotenv import load_dotenv

# Load environment variables explicitly from the root directory
env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
load_dotenv(dotenv_path=env_path)


# Initialize Logfire
try:
    token = os.getenv("LOGFIRE_TOKEN")
    if not token:
        print("ERROR: LOGFIRE_TOKEN is empty or None!")
    logfire.configure(token=token)
    # logfire.instrument_requests() # Disabled due to OpenTelemetry bug on Windows:
    # MeterProvider.get_meter() got multiple values for argument 'version'
    LOGFIRE_STATUS = "Connected & Tracing"
except Exception as e:
    print(f"Logfire Init Error in UI: {e}")
    LOGFIRE_STATUS = f"Standby (Error: {e})"


# --- PAGE CONFIG ---
st.set_page_config(
    page_title="Evidence Workspace",
    page_icon="✦",
    layout="wide",
)

st.markdown(
    """
    <style>
      .stApp { background: radial-gradient(circle at 15% 8%, #eee9ff 0, #faf9ff 28%, #ffffff 72%); }
      .block-container { max-width: 1180px; padding-top: 2.25rem; }
      .workspace-eyebrow { color: #6d4aff; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; font-size: .78rem; }
      .workspace-title { color: #160c55; font-size: 2.7rem; font-weight: 750; margin: .2rem 0; }
      .workspace-copy { color: #6b6880; font-size: 1.08rem; margin-bottom: 1.3rem; }
      .stButton button { border-radius: 12px; font-weight: 650; min-height: 2.8rem; }
      [data-testid="stChatInput"] { border-radius: 16px; border: 1px solid #ddd7fa; box-shadow: 0 8px 24px rgba(80, 55, 180, .08); }
      [data-testid="stFileUploader"] { border: 1.5px dashed #b7a8ff; border-radius: 16px; background: #fcfbff; padding: 1.4rem; }
      [data-testid="stFileUploaderDropzone"] { background: transparent; }
    </style>
    """,  # noqa: E501 -- inline CSS; one rule per line is easier to read
    unsafe_allow_html=True,
)

# --- AVATARS ---
AI_AVATAR = "🤖"
USER_AVATAR = "👤"


# --- SESSION MANAGEMENT ---
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
    logfire.info(f"✨ New User Session Created: {st.session_state.session_id}")

if "messages" not in st.session_state:
    st.session_state.messages = []

if "uploaded_documents" not in st.session_state:
    st.session_state.uploaded_documents = []


def _backend_url() -> str:
    return os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")


# Self-RAG verifier verdict → badge under technical answers (None → no badge).
GROUNDED_BADGES = {
    True: ("success", "✅ Verified against sources"),
    False: ("warning", "⚠️ Not fully supported by sources"),
}


def render_grounded_badge(grounded) -> None:
    badge = GROUNDED_BADGES.get(grounded) if isinstance(grounded, bool) else None
    if badge:
        kind, label = badge
        getattr(st, kind)(label)


RATE_LIMITED_MESSAGE = "⏳ You're sending requests faster than the service allows. Please wait a moment and try again."
# Self-correction can add grading, a rewrite + second retrieval, and verification.
QUERY_TIMEOUT_SECONDS = 120


class RateLimitedError(Exception):
    """The backend answered 429."""


class StreamUnavailableError(Exception):
    """/query/stream could not be used; the caller falls back to /query."""


def iter_sse(lines):
    """Parses Server-Sent Event lines into (event, data) pairs (data is JSON)."""
    event, data_lines = "message", []
    for line in lines:
        if line is None:
            continue
        if line == "":
            if data_lines:
                yield event, json.loads("\n".join(data_lines))
            event, data_lines = "message", []
        elif line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())


def stream_query(payload: dict, status, answer_placeholder) -> dict:
    """
    Calls /query/stream: shows step events in the status panel and tokens in the
    answer placeholder as they arrive. Returns a /query-shaped dict.
    Raises RateLimitedError on 429 and StreamUnavailableError when nothing was received.
    """
    data = {"answer": "", "sources": [], "thought_process": [], "grounded": None, "status": ""}
    received_any = False
    try:
        with requests.post(
            f"{_backend_url()}/query/stream", json=payload, stream=True, timeout=QUERY_TIMEOUT_SECONDS
        ) as response:
            if response.status_code == 429:
                raise RateLimitedError()
            if response.status_code != 200:
                raise StreamUnavailableError(f"HTTP {response.status_code}")
            for event, body in iter_sse(response.iter_lines(decode_unicode=True)):
                received_any = True
                if event == "step":
                    status.write(f"⚙️ {body.get('step')}")
                elif event == "token":
                    data["answer"] += body.get("text", "")
                    answer_placeholder.markdown(data["answer"] + "▌")
                elif event == "sources":
                    data["sources"] = body.get("sources") or []
                elif event == "done":
                    data.update({k: body.get(k) for k in ("status", "grounded", "thought_process")})
                    data["answer"] = body.get("answer") or data["answer"]
                    return data
                elif event == "error":
                    data.update(answer=body.get("message", "Something went wrong."), status="error")
                    return data
    except (requests.RequestException, ValueError) as exc:  # ValueError: malformed JSON
        if not received_any:
            raise StreamUnavailableError(str(exc)) from exc
        logfire.error(f"Stream interrupted: {exc}")
    if not received_any:
        raise StreamUnavailableError("empty stream")
    # Interrupted after part of the answer arrived. Retrying via /query would run the
    # question a second time, so keep what we have.
    data["status"] = "error"
    data["answer"] += "\n\n_⚠️ The connection was interrupted before the answer finished._"
    return data


def post_query(payload: dict) -> dict:
    """Non-streaming fallback: POST /query."""
    response = requests.post(f"{_backend_url()}/query", json=payload, timeout=QUERY_TIMEOUT_SECONDS)
    if response.status_code == 429:
        raise RateLimitedError()
    response.raise_for_status()
    return response.json()


def render_sources(sources) -> None:
    if not sources:
        return
    with st.expander("📄 View Retrieved Context (Sources)"):
        for i, source in enumerate(sources):
            source_name = source.get("source", "Unknown source")
            score = source.get("rerank_score")
            if score is None:
                score = source.get("retrieval_score")
            score_label = f" | relevance: {score:.3f}" if isinstance(score, float) else ""
            # [n] matches the inline citations in the answer.
            with st.expander(f"[{i + 1}] {source_name}{score_label}"):
                st.caption(f"Type: {source.get('source_type', 'unknown')}")
                st.info(source.get("content", ""))


@st.dialog("Upload and attach files", width="large")
def upload_documents_dialog():
    st.caption("Add knowledge sources to the workspace. Files are indexed with source metadata for traceable answers.")
    uploads = st.file_uploader(
        "Drop documents here or browse your device",
        type=["pdf", "html", "htm", "txt", "docx", "pptx"],
        accept_multiple_files=True,
        help="Up to 25 MB per document. Supported: PDF, HTML, TXT, DOCX, and PPTX.",
    )

    if uploads:
        st.markdown(f"#### {len(uploads)} file{'s' if len(uploads) != 1 else ''} ready to attach")
        for upload in uploads:
            left, right = st.columns([5, 1])
            left.markdown(f"**{upload.name}**  \\n+`{upload.size / (1024 * 1024):.2f} MB` • Ready for indexing")
            right.success("Ready")

    action_left, action_right = st.columns(2)
    if action_left.button("Cancel", width="stretch"):
        st.rerun()
    if action_right.button("Attach and index files", type="primary", width="stretch", disabled=not uploads):
        progress = st.progress(0, text="Preparing secure upload…")
        indexed = []
        try:
            for position, upload in enumerate(uploads, start=1):
                progress.progress(
                    int(((position - 1) / len(uploads)) * 100),
                    text=f"Indexing {upload.name} ({position}/{len(uploads)})…",
                )
                response = requests.post(
                    f"{_backend_url()}/documents",
                    files={"files": (upload.name, upload.getvalue(), upload.type)},
                    timeout=180,
                )
                response.raise_for_status()
                indexed.extend(response.json())
            st.session_state.uploaded_documents.extend(indexed)
            progress.progress(100, text="Documents indexed successfully")
            st.success(f"{len(indexed)} document{'s' if len(indexed) != 1 else ''} added to the knowledge workspace.")
        except requests.HTTPError as exc:
            progress.empty()
            if exc.response is not None and exc.response.status_code == 429:
                st.warning("⏳ Too many uploads in a short time. Please wait a minute and try again.")
                return
            try:
                detail = exc.response.json().get("detail", str(exc))
            except ValueError:
                detail = str(exc)
            st.error(f"Upload could not be completed: {detail}")
        except requests.RequestException as exc:
            progress.empty()
            st.error(f"Upload could not be completed: {exc}")


# --- SIDEBAR ---
with st.sidebar:
    st.title("✦ Evidence Workspace")
    st.markdown("---")
    st.caption("Source-grounded AI for technical knowledge")
    if st.button("＋ Upload knowledge sources", width="stretch", type="primary"):
        upload_documents_dialog()
    st.success(f"Tracing: {LOGFIRE_STATUS}")
    st.info(f"Conversation: {st.session_state.session_id[:8]}")

    if st.button("🗑️ Clear History & Memory", width="stretch", type="primary"):
        logfire.warn(f"🗑️ Memory Wipe Triggered for session: {st.session_state.session_id}")
        # Delete the server-side conversation memory, not just the chat on screen.
        try:
            reset_url = f"{_backend_url()}/threads/{st.session_state.session_id}/reset"
            requests.post(reset_url, timeout=15).raise_for_status()
        except requests.RequestException as exc:
            # The new session id below still starts a fresh conversation.
            logfire.error(f"Server-side memory reset failed: {exc}")
        st.session_state.messages = []
        st.session_state.session_id = str(uuid.uuid4())
        st.rerun()

# --- MAIN CHAT ---
st.markdown('<div class="workspace-eyebrow">Enterprise document intelligence</div>', unsafe_allow_html=True)
headline, upload_action = st.columns([4, 1])
headline.markdown('<div class="workspace-title">Ask questions with evidence.</div>', unsafe_allow_html=True)
if upload_action.button("Upload files", type="primary", width="stretch"):
    upload_documents_dialog()
st.markdown(
    '<div class="workspace-copy">Search trusted documents, inspect the retrieved context, '
    "and keep every answer traceable to its source.</div>",
    unsafe_allow_html=True,
)

if st.session_state.uploaded_documents:
    with st.expander(f"Workspace sources · {len(st.session_state.uploaded_documents)} indexed", expanded=False):
        for document in st.session_state.uploaded_documents:
            st.markdown(f"**{document['filename']}** · {document['chunks_indexed']} chunks indexed")


# Display history
for message in st.session_state.messages:
    avatar = AI_AVATAR if message["role"] == "assistant" else USER_AVATAR
    with st.chat_message(message["role"], avatar=avatar):
        st.markdown(message["content"])
        render_grounded_badge(message.get("grounded"))

# Chat Input
if prompt := st.chat_input("Ask a question about your knowledge sources…"):
    # START TRACE: User Interaction
    with logfire.span("💬 User Chat Interaction", user_query=prompt, session_id=st.session_state.session_id):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user", avatar=USER_AVATAR):
            st.markdown(prompt)

        # Assistant Response
        with st.chat_message("assistant", avatar=AI_AVATAR):
            payload = {"q": prompt, "thread_id": st.session_state.session_id}
            status = st.status("🔍 Agent is thinking...", expanded=True)
            # Created after the status panel, so tokens render below the live steps.
            answer_placeholder = st.empty()
            try:
                # DISTRIBUTED TRACE: Calling Backend
                with logfire.span("📡 Calling RAG Backend (stream)"):
                    try:
                        data = stream_query(payload, status, answer_placeholder)
                    except StreamUnavailableError as exc:
                        logfire.warning(f"Streaming unavailable ({exc}); falling back to /query")
                        data = post_query(payload)
                        # Reasoning steps from the backend (intent, each retrieval attempt,
                        # grading result, query rewrite, verification)
                        for step in data.get("thought_process") or []:
                            status.write(f"⚙️ {step}")
            except RateLimitedError:
                status.update(label="⏳ Rate limited", state="error", expanded=False)
                st.warning(RATE_LIMITED_MESSAGE)
                st.stop()
            except Exception as e:
                logfire.error(f"❌ UI-Backend Connection Failed: {e}")
                status.update(label="❌ Connection Failed", state="error")
                st.error("Backend Offline.")
                st.stop()

            failed = data.get("status") == "error"
            status.update(
                label="❌ Something went wrong" if failed else "✅ Answer Synthesized",
                state="error" if failed else "complete",
                expanded=False,
            )
            full_answer = data.get("answer") or "No response."
            answer_placeholder.markdown(full_answer)
            render_sources(data.get("sources", []))
            grounded = data.get("grounded")
            render_grounded_badge(grounded)
            st.session_state.messages.append({"role": "assistant", "content": full_answer, "grounded": grounded})
            logfire.info("✅ Chat cycle completed successfully.")
