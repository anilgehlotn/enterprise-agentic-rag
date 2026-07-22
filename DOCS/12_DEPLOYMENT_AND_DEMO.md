# Deployment and Interview Demo Guide

## What this proves

This project demonstrates a complete AI solution: local document ingestion, vector retrieval, reranking, guardrails, a FastAPI service, a Streamlit interface, evaluation workflows, and container packaging.

## Local demo

1. Copy `.env.example` to `.env` and set the required service keys.
2. Install application dependencies: `pip install -r requirements.txt`.
3. Index the supplied knowledge corpus: `python -m app.ingestion.processor DATA --wipe`.
4. Start the API: `uvicorn app.main:app --reload --port 8000`.
5. Check readiness at `http://localhost:8000/health`.
6. In another terminal, start the chat UI: `streamlit run ui/app.py`.

## Container readiness

The API container is defined in `Dockerfile` and exposes port `8080`.

```bash
docker build -t enterprise-agentic-rag .
docker run --env-file .env -p 8080:8080 enterprise-agentic-rag
curl http://localhost:8080/health
```

## Deployment plan

Before a public deployment, create a Qdrant collection and configure the required environment variables in the host's secret manager. Deploy the container API first, then point `BACKEND_URL` in the Streamlit service at the deployed API. Do not deploy `.env`, datasets with restricted content, or service keys to GitHub.

## Interview walkthrough

Use a question from `DATA/true_data/`. Explain that the planner classifies the request, Qdrant retrieves candidates, FlashRank reranks them, and the response includes attributed evidence. Then show `/health`, the Streamlit source panel, and the test workflow in GitHub Actions.
