# Architecture

Diagrams of the current system. Node and module names match the code: `app/agents/graph.py`, `app/services/rag_service.py`, `app/main.py`, `app/mcp_server.py`.

## 1. System overview

```mermaid
graph LR
    subgraph CLIENTS ["Clients"]
        direction TB
        CHAT["Streamlit chat<br/>ui/app.py"]
        EVAL_UI["Streamlit eval app<br/>evals/app.py"]
        MCPC["MCP client<br/>Claude Desktop / Code"]
    end

    subgraph ENTRY ["Entry points"]
        direction TB
        API["FastAPI<br/>/query · /query/stream (SSE)<br/>/documents · /threads/{id}/reset"]
        RL["Per-IP rate limit<br/>app/rate_limit.py"]
        MCPS["MCP server<br/>ask · search · list"]
    end

    subgraph CORE ["Shared service layer"]
        direction TB
        SVC["rag_service<br/>answer_question · stream_answer<br/>search_knowledge · reset_thread"]
        GR{"NeMo Guardrails<br/>fail-closed"}
    end

    subgraph AGENT ["LangGraph agent"]
        direction TB
        NODES["planner → retriever → grader<br/>→ rewriter ↺ → responder → verifier"]
        CP[("Checkpointer<br/>SQLite · Postgres · memory")]
    end

    subgraph RETRIEVAL ["Retrieval"]
        direction TB
        GEM["Gemini query embedding<br/>3072-dim"]
        BM["fastembed BM25<br/>query vector"]
        QD[("Qdrant<br/>dense + bm25 vectors<br/>RRF fusion")]
        FR["FlashRank<br/>TinyBERT cross-encoder"]
    end

    subgraph LLM ["LLM gateway"]
        direction TB
        PK["Portkey<br/>retry · fallback · cache"]
        G1["Groq llama-3.3-70b-versatile"]
        G2["Groq llama-3.1-8b-instant<br/>(fallback)"]
    end

    CHAT -->|"SSE, falls back to /query"| API
    EVAL_UI -->|"/query"| API
    MCPC --> MCPS
    API --> RL --> SVC
    MCPS --> SVC
    SVC --> GR
    GR -->|"pass"| NODES
    GR -->|"rail fired: canned reply"| SVC
    NODES <--> CP
    NODES --> GEM --> QD
    NODES --> BM --> QD
    QD --> FR --> NODES
    NODES --> PK
    PK --> G1
    PK -.->|"fallback"| G2
    GR -.->|"direct ChatGroq"| G2
```

The API and the MCP server never call the agent directly. Both go through `rag_service`, so guardrails, the per-request state reset, the thread config and the response shape are written once. `/query` and `/query/stream` run the same helpers: `/query` calls `graph.invoke()`, while `/query/stream` calls `graph.stream(stream_mode=["updates", "custom"])`.

## 2. Agent graph (self-correcting mode, the default)

```mermaid
flowchart TD
    START(["new_request_state(question)"]) --> planner
    planner -->|"CONVERSATIONAL"| responder
    planner -->|"refined search query"| retriever
    retriever -->|"RetrievalError:<br/>knowledge base unavailable"| E1(["END"])
    retriever -->|"top 15 → FlashRank → top 5"| grader
    grader -->|"≥ 1 relevant chunk"| responder
    grader -->|"0 relevant, attempts < 2"| rewriter
    rewriter -->|"new query"| retriever
    grader -->|"0 relevant, attempts = 2"| responder
    responder -->|"technical answer with documents"| verifier
    responder -->|"conversational, or fixed<br/>'not in the documents' reply"| E2(["END"])
    verifier -->|"grounded = true / false / null"| E3(["END"])
```

| Node | Module | LLM call | Writes |
|---|---|---|---|
| `planner` | `nodes/planner.py` | Portkey (LangChain `ChatOpenAI`) | `current_query` (search query or `CONVERSATIONAL`), `plan` |
| `retriever` | `nodes/retriever.py` | none (Gemini + BM25 embeddings, Qdrant, FlashRank) | `documents`, `retrieval_attempts`, or `retrieval_error` + the unavailable answer |
| `grader` | `nodes/grader.py` | one batched call for all chunks; fails open | `documents` (relevant only) |
| `rewriter` | `nodes/rewriter.py` | one small call; falls back to the original question | `current_query` |
| `responder` | `nodes/responder.py` | Portkey native client (`stream=True` on `/query/stream`); none for the no-evidence reply | `final_answer`, `messages`, `status` |
| `verifier` | `nodes/verifier.py` | one call; labels the answer, never changes it | `grounded` |

The routing functions are pure and live in `app/agents/routing.py`. With `ENABLE_SELF_CORRECTION=false` the graph is `planner → (retriever) → responder → END`, with no grader, rewriter or verifier.

## 3. Streaming (`POST /query/stream`)

```mermaid
sequenceDiagram
    participant UI as Streamlit UI
    participant API as FastAPI
    participant SVC as rag_service.stream_answer
    participant G as LangGraph
    participant R as responder node
    participant P as Portkey (stream=True)

    UI->>API: POST /query/stream {q, thread_id}
    API->>API: rate limit, validate body (429 / 422 before streaming)
    API->>SVC: start generator
    SVC->>SVC: guardrails (blocked → canned reply as tokens → done)
    SVC->>G: stream(stream_mode=["updates", "custom"])
    G-->>SVC: "updates": planner, retriever, grader finished
    SVC-->>UI: event: step (one per new plan entry)
    G->>R: run responder
    R->>P: chat.completions.create(stream=True)
    P-->>R: text chunks
    R-->>G: get_stream_writer()({"type": "token", ...})
    G-->>SVC: "custom" items
    SVC-->>UI: event: token
    G-->>SVC: "updates": responder, verifier finished
    SVC-->>UI: event: step, event: sources, event: done
```

Answers that aren't written by the LLM (guardrail reply, "knowledge base unavailable", "not in the documents") are sent as word-sized `token` events after the graph finishes. Any exception becomes a single `error` event with a generic message. The UI falls back to `POST /query` only if the stream can't be opened or delivers nothing.

## 4. Conversation memory

```mermaid
flowchart LR
    REQ["request with thread_id"] --> STATE["new_request_state()<br/>resets per-request fields,<br/>appends the user message"]
    STATE --> RUN["graph run"]
    RUN -->|"checkpoint after every node"| CP[("Checkpointer")]
    CP -->|"latest state for thread_id"| RUN
    RESET["POST /threads/{id}/reset"] -->|"delete_thread()"| CP

    subgraph BACKENDS ["CHECKPOINTER"]
        M["memory<br/>in-process (tests)"]
        S["sqlite (default)<br/>&lt;project root&gt;/state/checkpoints.sqlite"]
        PG["postgres<br/>DATABASE_URL, psycopg pool<br/>(Cloud Run)"]
    end
    CP --- BACKENDS
```

The checkpointer is opened by the FastAPI lifespan at startup (an invalid `CHECKPOINTER` fails startup; Postgres runs `setup()` here) and closed on shutdown. The MCP server opens it lazily on first use.

## 5. Ingestion

```mermaid
flowchart LR
    CLI["python -m app.ingestion.processor DATA --wipe"] --> PF["process_file()"]
    UP["POST /documents<br/>extension, size, rate-limit checks"] --> PF
    PF --> L{"loader by extension"}
    L -->|"pdf"| PDF["pypdf, pdfplumber fallback"]
    L -->|"html / htm"| HTML["BeautifulSoup"]
    L -->|"txt"| TXT["text"]
    L -->|"docx / pptx"| OFF["python-docx / python-pptx"]
    PDF & HTML & TXT & OFF --> CH["chunk_text()<br/>paragraph-aware, ≤ 1,500 chars"]
    CH --> JSON[("processed_data/&lt;source_type&gt;/*.json")]
    CH --> DE["Gemini dense vectors<br/>batches of 50, backoff"]
    CH --> SP["fastembed BM25 sparse vectors"]
    DE & SP --> QD[("Qdrant: one point per chunk<br/>named vectors dense + bm25<br/>payload text, source, source_type")]
```

`ensure_collection()` is the only code that creates the collection. A collection with the old single-vector schema is refused (CLI error, HTTP 409 on upload) instead of mixing formats.

## 6. Delivery

```mermaid
flowchart LR
    PUSH["push / PR"] --> CI["ci.yml"]
    CI --> LINT["ruff check + format"]
    CI --> TEST["pytest + coverage gate"]
    CI --> DOCK["docker build + GET /health"]
    MANUAL["manual run"] --> CD["deploy-cloud-run.yml"]
    CD --> WIF["Workload Identity Federation"]
    WIF --> AR["build + push image<br/>tag = git SHA"]
    AR --> CR["Cloud Run<br/>secrets from Secret Manager<br/>max 2 instances"]
    CR --> SMOKE["GET /health gate"]
```

## 7. Evaluation and observability

```mermaid
flowchart LR
    GD[("evals/golden_dataset.json<br/>15 RAG samples, 6 guardrail cases")] --> P1["Phase 1: POST /query<br/>evals/pipeline.py"]
    GD --> GE["guardrails_eval.py<br/>TP / TN / FP / FN"]
    P1 --> P2["Phase 2: RAGAS<br/>faithfulness, relevancy, precision,<br/>recall, correctness"]
    P1 --> TC["tool correctness<br/>Jaccard, no LLM"]
    P2 --> J["judge: Groq llama-3.1-8b-instant<br/>JUDGE_GROQ"]
    GD --> RB["retrieval_benchmark.py<br/>dense vs hybrid, ± FlashRank"]

    APP["API, agent, ingestion"] -.->|"spans (if LOGFIRE_TOKEN)"| LF["Logfire"]
    APP -.->|"LangChain / LangGraph runs"| LS["LangSmith"]
    APP -.->|"every gateway call"| PKD["Portkey logs"]
```
