import os
import streamlit as st
import requests
import time
import uuid
import logfire
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
    # logfire.instrument_requests() # Disabled due to OpenTelemetry bug on Windows: MeterProvider.get_meter() got multiple values for argument 'version'
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
    """,
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
        st.session_state.messages = []
        st.session_state.session_id = str(uuid.uuid4())
        st.rerun()

# --- MAIN CHAT ---
st.markdown('<div class="workspace-eyebrow">Enterprise document intelligence</div>', unsafe_allow_html=True)
headline, upload_action = st.columns([4, 1])
headline.markdown('<div class="workspace-title">Ask questions with evidence.</div>', unsafe_allow_html=True)
if upload_action.button("Upload files", type="primary", width="stretch"):
    upload_documents_dialog()
st.markdown('<div class="workspace-copy">Search trusted documents, inspect the retrieved context, and keep every answer traceable to its source.</div>', unsafe_allow_html=True)

if st.session_state.uploaded_documents:
    with st.expander(f"Workspace sources · {len(st.session_state.uploaded_documents)} indexed", expanded=False):
        for document in st.session_state.uploaded_documents:
            st.markdown(f"**{document['filename']}** · {document['chunks_indexed']} chunks indexed")


# Display history
for message in st.session_state.messages:
    avatar = AI_AVATAR if message["role"] == "assistant" else USER_AVATAR
    with st.chat_message(message["role"], avatar=avatar):
        st.markdown(message["content"])

# Chat Input
if prompt := st.chat_input("Ask a question about your knowledge sources…"):
    # START TRACE: User Interaction
    with logfire.span("💬 User Chat Interaction", user_query=prompt, session_id=st.session_state.session_id):
        
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user", avatar=USER_AVATAR):
            st.markdown(prompt)

        # Assistant Response
        with st.chat_message("assistant", avatar=AI_AVATAR):
            with st.status("🔍 Agent is thinking...", expanded=True) as status:
                try:
                    # DISTRIBUTED TRACE: Calling Backend
                    with logfire.span("📡 Calling RAG Backend"):
                        # Get backend URL from env, or default to local if not set
                        url = f"{_backend_url()}/query"
                        payload = {"q": prompt, "thread_id": st.session_state.session_id}
                        response = requests.post(url, json=payload, timeout=60)
                        data = response.json()
                    
                    # Show Reasoning Steps from Backend
                    steps = data.get("thought_process", [])
                    for step in steps:
                        st.write(f"⚙️ {step}")
                    
                    status.update(label="✅ Answer Synthesized", state="complete", expanded=False)
                    
                    # --- SHOW SOURCES (NESTED EXPANDABLES) ---
                    sources = data.get("sources", [])
                    if sources:
                        with st.expander("📄 View Retrieved Context (Sources)"):
                            for i, source in enumerate(sources):
                                source_name = source.get("source", "Unknown source")
                                content = source.get("content", "")
                                score = source.get("rerank_score") or source.get("score")
                                score_label = f" | relevance: {score:.3f}" if isinstance(score, float) else ""
                                with st.expander(f"{i+1}. {source_name}{score_label}"):
                                    st.caption(f"Type: {source.get('source_type', 'unknown')}")
                                    st.info(content)
                except Exception as e:
                    logfire.error(f"❌ UI-Backend Connection Failed: {e}")
                    status.update(label="❌ Connection Failed", state="error")
                    st.error("Backend Offline.")
                    st.stop()

            # Final Answer Streaming
            answer_placeholder = st.empty()
            full_answer = data.get("answer", "No response.")
            
            curr_text = ""
            for char in full_answer:
                curr_text += char
                answer_placeholder.markdown(curr_text + "▌")
                time.sleep(0.005)
            
            answer_placeholder.markdown(full_answer)
            st.session_state.messages.append({"role": "assistant", "content": full_answer})
            logfire.info("✅ Chat cycle completed successfully.")
