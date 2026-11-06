# syntax=docker/dockerfile:1
# ==============================================================================
# Enterprise Agentic RAG API — production image (Cloud Run)
#
#   Stage 1 "builder": compilers + pip install into an isolated venv, then
#                      pre-download the ONNX models used at request time.
#   Stage 2 "runtime": slim Python + the venv + the models. No compilers, no pip
#                      cache, runs as a non-root user.
# ==============================================================================

ARG PYTHON_IMAGE=python:3.11-slim-bookworm

# ------------------------------------------------------------------------------
# Stage 1: builder
# ------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    MODEL_DIR=/opt/models \
    FASTEMBED_CACHE_PATH=/opt/models \
    FLASHRANK_CACHE_DIR=/opt/models

# Build-only toolchain for packages without a prebuilt wheel. Never reaches the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "$VIRTUAL_ENV"

# uv resolves requirements-prod.txt in seconds. pip's backtracking resolver gives up on
# it (ResolutionImpossible while walking logfire's opentelemetry-instrumentation-* pins).
RUN pip install uv==0.11.30

# Requirements first: this layer is rebuilt only when requirements-prod.txt changes.
COPY requirements-prod.txt .
RUN uv pip install --python "$VIRTUAL_ENV/bin/python" --no-cache -r requirements-prod.txt

# Pre-download every model loaded at request time / startup so a Cloud Run cold
# start never hits the network for them:
#   - FlashRank cross-encoder (ms-marco-TinyBERT-L-2-v2, FlashRank's default)
#   - fastembed BM25 sparse model (Qdrant/bm25) used by hybrid retrieval
#   - fastembed all-MiniLM-L6-v2, used by NeMo Guardrails to embed Colang intents
# The names must match app/services/retrieval/schema.py (BM25_MODEL) and
# NeMo's default embedding model.
RUN python - <<'PY'
import os
from fastembed import SparseTextEmbedding, TextEmbedding
from flashrank import Ranker

cache = os.environ["MODEL_DIR"]
Ranker(cache_dir=cache)
list(SparseTextEmbedding(model_name="Qdrant/bm25").embed(["warm up"]))
list(TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2").embed(["warm up"]))
print("Models cached in", cache, ":", sorted(os.listdir(cache)))
PY

# ------------------------------------------------------------------------------
# Stage 2: runtime
# ------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    FASTEMBED_CACHE_PATH=/opt/models \
    FLASHRANK_CACHE_DIR=/opt/models \
    HOME=/home/app \
    PORT=8080

# Patch OS-level CVEs; libgomp1 is the OpenMP runtime needed by onnxruntime.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged user with a fixed UID (useful for Kubernetes runAsNonRoot checks).
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /home/app --create-home app

COPY --from=builder /opt/venv /opt/venv
# Owned by app: the model loaders may write lock/metadata files next to the models.
COPY --from=builder --chown=app:app /opt/models /opt/models

WORKDIR /app
# /documents uploads write processed_data/ under the working dir, so the app user owns
# /app. (Cloud Run's filesystem is in-memory and per-instance: this is scratch space.)
RUN chown app:app /app
# Only the app package — evals/, ui/, DATA/, DOCS/, tests/ stay out (see .dockerignore).
COPY --chown=app:app app/ ./app/

USER app

EXPOSE 8080

# Docker / Compose health status. Cloud Run ignores HEALTHCHECK and uses its own
# startup probe on $PORT; slim images ship no curl, so use Python's urllib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/health', timeout=4)" || exit 1

# Cloud Run injects $PORT. `exec` makes uvicorn PID 1 so it receives SIGTERM directly
# (and runs the FastAPI lifespan shutdown, which closes the checkpointer connection).
# --no-proxy-headers: uvicorn would otherwise rewrite the client address from
# X-Forwarded-For whenever the peer is in FORWARDED_ALLOW_IPS (default 127.0.0.1), behind
# the app's back. The app decides that itself via TRUST_PROXY_HEADERS (app/rate_limit.py).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --no-proxy-headers"]
