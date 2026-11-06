# Enterprise Agentic RAG

[![CI](https://github.com/anilgehlotn/enterprise-agentic-rag/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/anilgehlotn/enterprise-agentic-rag/actions/workflows/ci.yml)

An evidence-first AI assistant for technical document intelligence. It ingests mixed enterprise documents, retrieves and reranks the most relevant evidence, applies safety guardrails, and delivers source-attributed answers through a FastAPI service and Streamlit interface.

> **Live demo:** [Open Enterprise Agentic RAG](https://anil-enterprise-rag.streamlit.app/)

Use the demo to upload a supported document, ask a technical question, and inspect the retrieved evidence that supports the answer.

## Why this project

Enterprise teams need answers they can verify, not a generic chatbot. This project demonstrates an end-to-end RAG application with:

- document ingestion for PDF, HTML, TXT, DOCX, and PPTX files;
- hybrid search in Qdrant: Gemini dense embeddings + BM25 keyword vectors, fused with Reciprocal Rank Fusion;
- local cross-encoder reranking with FlashRank;
- agentic routing for conversational versus knowledge-seeking questions;
- NeMo guardrails for off-topic, jailbreak, and prompt-injection attempts;
- source-aware answers, automated tests, CI, evaluation workflows, and container readiness.

## At a glance

| Area | Implementation |
|---|---|
| API | FastAPI with typed `/query` and `/health` endpoints |
| Agent orchestration | LangGraph planner, retriever, and responder nodes with thread memory |
| Retrieval | Hybrid dense (Gemini) + sparse (BM25) search with Qdrant RRF fusion, then FlashRank reranking |
| LLM access | Portkey gateway with Groq primary/fallback routing |
| Safety | NVIDIA NeMo Guardrails before retrieval and generation |
| User experience | Streamlit chat interface with retrieved-source visibility |
| Quality | Golden-dataset evaluation suite, unit tests, and GitHub Actions CI |
| Delivery | Dockerfile and deployment/demo runbook |

## How it works

```mermaid
flowchart LR
    U[User question] --> UI[Streamlit UI]
    UI --> API[FastAPI /query]
    API --> G{NeMo Guardrails}
    G -->|blocked| B[Safe response]
    G -->|allowed| P[LangGraph planner]
    P -->|conversation| R[Responder]
    P -->|technical query| Q[Qdrant hybrid search<br/>dense + BM25 → RRF]
    Q --> F[FlashRank reranker]
    F --> R
    R --> A[Answer with evidence]
    A --> UI
```

For technical questions, the retriever preserves each chunk's filename, source type, retrieval score, and reranking score. The UI exposes this evidence alongside the answer, making the RAG behavior easier to inspect and trust.

## Engineering highlights

### Evidence-first retrieval

The pipeline keeps document metadata through retrieval and reranking instead of returning anonymous text chunks. This makes answers traceable to their source documents and gives users a practical way to validate generated output.

### Safety before generation

NeMo Guardrails runs before the RAG workflow. Blocked inputs skip retrieval and generation, reducing exposure to prompt injection, jailbreak attempts, and unrelated requests.

### Evaluation and quality gates

The `evals/` package includes a golden dataset, guardrail test cases, RAGAS-based scoring, and a Streamlit evaluation dashboard. Focused unit tests cover source attribution and chunking behavior; GitHub Actions runs them on pushes and pull requests.

## Repository map

```text
app/
  agents/        LangGraph planner, retriever, responder, and state
  ingestion/     Document parsing, chunking, and vector indexing
  services/      Embeddings, Qdrant retrieval, reranking, citations
  guardrails/    NeMo safety policies
  gateway/       Portkey-backed LLM client
  main.py        FastAPI application
  mcp_server.py  MCP server (read-only tools for Claude Desktop / Claude Code)
ui/              Streamlit chat experience
evals/           Golden dataset, evaluation pipeline, and metrics dashboard
tests/           Fast unit tests for core retrieval utilities
DATA/            Sample true-data and noisy-data corpora
DOCS/            Architecture, operations, evaluation, and deployment guides
```

## Run locally

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
```

### 2. Configure services

Copy `.env.example` to `.env` and provide the required credentials:

```env
GEMINI_API_KEY=
QDRANT_API_KEY=
QDRANT_CLUSTER_ENDPOINT=
GROQ_API_KEY=
GROQ_FALLBACK_API_KEY=
PORTKEY_API_KEY=
LOGFIRE_TOKEN=
LANGSMITH_API_KEY=
```

Never commit `.env` or API keys.

### 3. Ingest the sample corpus

```bash
python -m app.ingestion.processor DATA --wipe
```

Each chunk is stored with a dense Gemini vector and a BM25 sparse vector. Collections created before hybrid search must be re-ingested with `--wipe` (the app tells you so instead of mixing formats). Set `RETRIEVAL_MODE=dense` in `.env` to switch off the keyword half; the default is `hybrid`.

### 4. Start the API and UI

```bash
# Terminal 1
uvicorn app.main:app --reload --port 8000

# Terminal 2
streamlit run ui/app.py
```

Confirm the API is ready:

```bash
curl http://localhost:8000/health
```

## MCP server

The knowledge base is also available as a [Model Context Protocol](https://modelcontextprotocol.io) server, so MCP clients such as Claude Desktop, Claude Code and VS Code can use it as a tool. It exposes three read-only tools: `ask_knowledge_base` (guardrails + self-correcting agent, cited and verified answer), `search_knowledge_base` (reranked evidence, no LLM) and `list_indexed_documents`.

```bash
python -m app.mcp_server                                           # stdio, for Claude Desktop / Claude Code
python -m app.mcp_server --transport http --host 127.0.0.1 --port 8001   # streamable HTTP
claude mcp add enterprise-rag --env PYTHONPATH=$PWD -- $PWD/.venv/bin/python -m app.mcp_server
```

See the [MCP server guide](DOCS/13_MCP_SERVER.md) for Claude Desktop config, MCP Inspector and security notes.

## Retrieval benchmark

Dense embeddings blur exact technical terms (kubectl flags, YAML fields such as `backoffLimit` or `parallelism`), so retrieval is hybrid: dense top 30 + BM25 top 30, fused with Qdrant's native RRF, then FlashRank picks the top 5. `evals/retrieval_benchmark.py` measures whether that helps on the 15 golden questions, against the live collection:

```bash
python -m evals.retrieval_benchmark
```

| Configuration | Hit rate@5 | MRR@5 | Noise rate@5 ↓ | Avg latency (ms) |
|---|---:|---:|---:|---:|
| Dense only | _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| Dense + FlashRank | _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| Hybrid (RRF) | _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| Hybrid + FlashRank (default) | _TBD_ | _TBD_ | _TBD_ | _TBD_ |

_Date, collection size and embedding model: see `evals/results/retrieval_benchmark.md`._

- **Hit rate@5**: share of questions with at least one golden relevant chunk in the top 5.
- **MRR@5**: mean of 1 / rank of the first relevant chunk (0 if none in the top 5).
- **Noise rate@5**: share of top-5 chunks from the distractor (`noisy`) corpus; lower is better.
- A chunk counts as relevant if, after normalising case and whitespace, it contains a golden context (or vice versa) or shares ≥ 60% of its word bigrams; see `evals/retrieval_metrics.py`.
- 15 questions is a small sample: one question moves hit rate by 6.7 points, so treat small differences as noise.

## Quality checks

```bash
pip install -r requirements-dev.txt
pytest tests -q
```

For retrieval quality and guardrail behavior, run the evaluation interface after starting the API:

```bash
streamlit run evals/app.py
```

## Deployment

The public Streamlit experience is available at [anil-enterprise-rag.streamlit.app](https://anil-enterprise-rag.streamlit.app/). The FastAPI service is containerized with the provided `Dockerfile` and exposes a `/health` endpoint for deployment checks. See [Deployment and Demo Guide](DOCS/12_DEPLOYMENT_AND_DEMO.md) for the local demo flow, container commands, and a deployment checklist.

## Documentation

- [System architecture](ARCHITECTURE.md)
- [Ingestion engine](DOCS/02_INGESTION_ENGINE.md)
- [Agent decision flow](DOCS/03_NODE_INTELLIGENCE.md)
- [Guardrails](DOCS/08_GUARDRAILS.md)
- [LLM gateway](DOCS/09_LLM_GATEWAY.md)
- [Evaluation methodology](DOCS/10_EVALS.md)
- [Deployment and interview demo](DOCS/12_DEPLOYMENT_AND_DEMO.md)
- [MCP server](DOCS/13_MCP_SERVER.md)
- [Project guide](DOCS/PROJECT_GUIDE.md)


