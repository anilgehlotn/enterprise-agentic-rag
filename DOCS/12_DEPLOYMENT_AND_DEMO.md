# Deployment Guide: CI, Cloud Run, and the Demo

This guide takes the API from a laptop to Google Cloud Run. Follow the sections in order. The one-time setup (sections 2 to 4) is done once per GCP project. After that, a deploy is one click.

```
GitHub push / PR ──► CI (.github/workflows/ci.yml)
                      ├─ lint    ruff check + ruff format --check
                      ├─ test    pytest + coverage gate, coverage.xml artifact
                      └─ docker  build the production image, run it, GET /health == ok

Manual "Run workflow" ──► CD (.github/workflows/deploy-cloud-run.yml)
                      auth (Workload Identity Federation, no JSON keys)
                      → build + push image to Artifact Registry (tag = git SHA)
                      → deploy to Cloud Run (API keys from Secret Manager)
                      → GET <service-url>/health must return ok
```

---

## 1. Local run (unchanged)

1. Copy `.env.example` to `.env` and fill in the keys. `.env` is git-ignored and docker-ignored. Never commit it.
2. `pip install -r requirements.txt`
3. Index the corpus: `python -m app.ingestion.processor DATA --wipe`
4. Start the API: `uvicorn app.main:app --reload --port 8000`, then open `http://localhost:8000/health`.
5. In another terminal: `streamlit run ui/app.py`

Run the CI checks locally before pushing:

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
python -m pytest tests -q --cov=app
```

Run the production image locally. `/health` needs no real keys. Startup only needs a non-empty `GROQ_API_KEY` (a dummy value is fine; the guardrails client is built at startup but makes no call until a request arrives):

```bash
docker build -t enterprise-agentic-rag .
docker run --rm -p 8080:8080 -e GROQ_API_KEY=dummy enterprise-agentic-rag
curl http://localhost:8080/health      # {"status":"ok","service":"enterprise-agentic-rag"}
# Real queries need real keys:
docker run --rm -p 8080:8080 --env-file .env enterprise-agentic-rag
```

---

## 2. One-time GCP setup

You need the [gcloud CLI](https://cloud.google.com/sdk/docs/install), a GCP project with billing enabled, and Owner (or equivalent) on that project for this one-time setup. Your day-to-day deploys then run as a narrowly scoped service account.

Set these shell variables first. Every command below uses them.

```bash
export PROJECT_ID="your-project-id"            # gcloud projects list
export REGION="asia-south1"                    # Mumbai; used for Artifact Registry and Cloud Run
export AR_REPO="enterprise-rag"                # Artifact Registry repository name
export SERVICE="enterprise-agentic-rag"        # Cloud Run service name (workflow default)
export GITHUB_REPO="anilgehlotn/enterprise-agentic-rag"
export DEPLOY_SA="github-deployer"             # used by GitHub Actions to deploy
export RUNTIME_SA="rag-runtime"                # identity the running container uses
export POOL="github-pool"
export PROVIDER="github-provider"

gcloud config set project "$PROJECT_ID"
export PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
```

### 2.1 Enable the APIs

```bash
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com
```

| API | Why |
|---|---|
| `run` | Cloud Run itself |
| `artifactregistry` | Stores the Docker images |
| `secretmanager` | Stores the API keys |
| `iam`, `iamcredentials` | Service accounts, and minting short-lived tokens for them |
| `sts` | Security Token Service: exchanges GitHub's OIDC token during Workload Identity Federation |

### 2.2 Create the Artifact Registry repository

```bash
gcloud artifacts repositories create "$AR_REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --description="Enterprise Agentic RAG images"
```

Optional: keep storage inside the free 0.5 GB by deleting old images. Add a cleanup policy in the console (Artifact Registry → repository → Cleanup policies), for example "keep the 5 most recent versions".

### 2.3 Create one Secret Manager secret per API key

The deploy workflow maps each environment variable to a secret with **the same name**. Create every secret below, even if you don't use the service yet; a missing secret makes the deploy fail. For a service you don't use, store a placeholder such as `unused`. Exception: `LOGFIRE_TOKEN` with a fake value makes Logfire log export errors. If you don't use Logfire, delete the `LOGFIRE_TOKEN` line from the workflow's `secrets:` block instead; with no token, telemetry is simply skipped. `GROQ_API_KEY` must be real: the API refuses to start without it, and the deploy's health check catches that.

```bash
for name in GROQ_API_KEY GROQ_FALLBACK_API_KEY PORTKEY_API_KEY GEMINI_API_KEY \
            QDRANT_API_KEY QDRANT_CLUSTER_ENDPOINT LOGFIRE_TOKEN LANGSMITH_API_KEY; do
  gcloud secrets create "$name" --replication-policy=automatic
done
```

Add the values. Read each key from the prompt so it never lands in your shell history:

```bash
for name in GROQ_API_KEY GROQ_FALLBACK_API_KEY PORTKEY_API_KEY GEMINI_API_KEY \
            QDRANT_API_KEY QDRANT_CLUSTER_ENDPOINT LOGFIRE_TOKEN LANGSMITH_API_KEY; do
  read -rsp "Value for $name: " value; echo
  printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=-
done
unset value
```

> **Never commit secrets.** Keys go only into Secret Manager (for Cloud Run) or your local, git-ignored `.env`. Not into GitHub secrets, workflow files, the Dockerfile, or the docs. If a key ever leaks, rotate it at the provider, then run `gcloud secrets versions add` with the new value and redeploy.

To rotate a key later: add a new version (same command as above), then re-run the deploy workflow. The service reads `:latest` on each new revision.

### 2.4 Create the service accounts (least privilege)

Two identities, so the thing that deploys and the thing that runs each hold only what they need.

```bash
gcloud iam service-accounts create "$DEPLOY_SA"  --display-name="GitHub Actions deployer"
gcloud iam service-accounts create "$RUNTIME_SA" --display-name="Cloud Run runtime for the RAG API"

export DEPLOY_SA_EMAIL="$DEPLOY_SA@$PROJECT_ID.iam.gserviceaccount.com"
export RUNTIME_SA_EMAIL="$RUNTIME_SA@$PROJECT_ID.iam.gserviceaccount.com"
```

**Deployer roles** (used by GitHub Actions):

```bash
# Push images, only to this one repository.
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" --location="$REGION" \
  --member="serviceAccount:$DEPLOY_SA_EMAIL" --role="roles/artifactregistry.writer"

# Create and update Cloud Run services, and set their IAM (needed for --allow-unauthenticated).
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$DEPLOY_SA_EMAIL" --role="roles/run.admin"

# Deploy a service that runs as the runtime SA ("act as"), on that one SA only.
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA_EMAIL" \
  --member="serviceAccount:$DEPLOY_SA_EMAIL" --role="roles/iam.serviceAccountUser"
```

| Role | Granted on | Why |
|---|---|---|
| `roles/artifactregistry.writer` | the `$AR_REPO` repository only | Push the image built by the workflow. Not project-wide, so it can't touch other repositories. |
| `roles/run.admin` | the project | Create and update the service, and set its IAM policy so `--allow-unauthenticated` works. If you always deploy private (`public: false`), `roles/run.developer` is enough. |
| `roles/iam.serviceAccountUser` | the runtime SA only | Cloud Run requires the deployer to "act as" the identity the service runs as. Scoped to that one account, not every SA in the project. |

**Runtime roles** (used by the running container):

```bash
for name in GROQ_API_KEY GROQ_FALLBACK_API_KEY PORTKEY_API_KEY GEMINI_API_KEY \
            QDRANT_API_KEY QDRANT_CLUSTER_ENDPOINT LOGFIRE_TOKEN LANGSMITH_API_KEY; do
  gcloud secrets add-iam-policy-binding "$name" \
    --member="serviceAccount:$RUNTIME_SA_EMAIL" --role="roles/secretmanager.secretAccessor"
done
```

| Role | Granted on | Why |
|---|---|---|
| `roles/secretmanager.secretAccessor` | each of the 8 secrets | Cloud Run reads the secret values as this identity when a revision starts. Granted per secret, so the app can't read unrelated secrets in the project. |

The runtime SA needs nothing else: Qdrant, Groq, Portkey and Gemini are reached with API keys over the internet, not through GCP IAM. Without a dedicated runtime SA, Cloud Run would use the default compute service account, which has the broad Editor role.

**Optional**, only if you deploy with `public: false`: the workflow's smoke test calls the private service with an ID token minted for the deploy SA.

```bash
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA_EMAIL" \
  --member="serviceAccount:$DEPLOY_SA_EMAIL" --role="roles/iam.serviceAccountTokenCreator"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$DEPLOY_SA_EMAIL" --role="roles/run.invoker"
```

### 2.5 Workload Identity Federation, restricted to this repository

GitHub Actions gets a signed OIDC token for every run. WIF lets Google trust that token and exchange it for a short-lived credential for the deploy SA. There is no JSON key to create, store, or leak.

```bash
# 1. A pool groups external identities.
gcloud iam workload-identity-pools create "$POOL" \
  --location=global --display-name="GitHub Actions"

# 2. The provider trusts GitHub's OIDC issuer, maps token claims to attributes, and
#    REJECTS every token whose repository isn't yours (attribute-condition).
gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
  --location=global \
  --workload-identity-pool="$POOL" \
  --display-name="GitHub OIDC" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository == '$GITHUB_REPO'"

# 3. Only workflows from this repository may impersonate the deploy SA.
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA_EMAIL" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL/attribute.repository/$GITHUB_REPO"

# 4. Print the provider resource name for the GitHub variable GCP_WIF_PROVIDER.
gcloud iam workload-identity-pools providers describe "$PROVIDER" \
  --location=global --workload-identity-pool="$POOL" --format='value(name)'
# → projects/123456789/locations/global/workloadIdentityPools/github-pool/providers/github-provider
```

The restriction is applied twice: the provider's `attribute-condition` rejects tokens from any other repository, and the `workloadIdentityUser` binding names your repository. To also restrict deploys to `main`, add `&& assertion.ref == 'refs/heads/main'` to the condition.

### 2.6 GitHub repository variables

GitHub → repository → **Settings → Secrets and variables → Actions → Variables** tab → *New repository variable*. These are identifiers, not secrets, so they go under **Variables**:

| Variable | Value |
|---|---|
| `GCP_PROJECT_ID` | `$PROJECT_ID` |
| `GCP_REGION` | `asia-south1` (the Artifact Registry location) |
| `GCP_WIF_PROVIDER` | the full name printed by step 2.5.4 |
| `GCP_SERVICE_ACCOUNT` | `github-deployer@<project-id>.iam.gserviceaccount.com` |
| `AR_REPOSITORY` | `enterprise-rag` |
| `CLOUD_RUN_RUNTIME_SA` | `rag-runtime@<project-id>.iam.gserviceaccount.com` (recommended; if unset, Cloud Run uses the default compute SA) |
| `CHECKPOINTER` | Optional. `postgres` for persistent conversation memory (needs the `DATABASE_URL` secret, see section 8). Unset = `sqlite`, which on Cloud Run is per instance and lost on scale-down. |

Or with the GitHub CLI:

```bash
gh variable set GCP_PROJECT_ID        --body "$PROJECT_ID"
gh variable set GCP_REGION            --body "$REGION"
gh variable set GCP_WIF_PROVIDER      --body "$(gcloud iam workload-identity-pools providers describe "$PROVIDER" --location=global --workload-identity-pool="$POOL" --format='value(name)')"
gh variable set GCP_SERVICE_ACCOUNT   --body "$DEPLOY_SA_EMAIL"
gh variable set AR_REPOSITORY         --body "$AR_REPO"
gh variable set CLOUD_RUN_RUNTIME_SA  --body "$RUNTIME_SA_EMAIL"
```

No GitHub *secrets* are needed for deployment.

The deploy job runs in a GitHub **environment** named `production`, which GitHub creates on the first run. In Settings → Environments → production you can add required reviewers, so every deploy waits for your approval.

---

## 3. Re-ingest the `enterprise_rag_v2` collection (before the first deploy)

Cloud Run is configured with `QDRANT_COLLECTION=enterprise_rag_v2` and `RETRIEVAL_MODE=hybrid`. That collection needs the hybrid schema: a named `dense` vector plus a `bm25` sparse vector with the IDF modifier. The API never creates or fills it on startup. A missing or old-schema collection makes queries fail with "Collection uses the old schema — re-ingest". So index the corpus once from your machine, against the same Qdrant cluster the secrets point to:

```bash
# .env must hold the real QDRANT_CLUSTER_ENDPOINT, QDRANT_API_KEY and GEMINI_API_KEY.
QDRANT_COLLECTION=enterprise_rag_v2 python -m app.ingestion.processor DATA --wipe
```

`--wipe` drops and recreates `enterprise_rag_v2` only. An older deployment that still reads `enterprise_rag` keeps working while you switch over.

Check it in the Qdrant console: the collection should exist, with `dense` and `bm25` vectors and a non-zero point count.

Ingest with Gemini embeddings (a working `GEMINI_API_KEY`). The production image doesn't ship the `sentence-transformers` fallback. A collection built with the 768-dim fallback won't match the 3072-dim query vectors in production.

---

## 4. Deploy

1. Make sure CI is green on the commit you want to ship.
2. GitHub → **Actions → Deploy to Cloud Run → Run workflow**.
3. Choose the branch (normally `main`), keep `region = asia-south1`, `service = enterprise-agentic-rag`, `public = true` for a browser demo, then click **Run workflow**.
4. The job authenticates through WIF, builds and pushes `REGION-docker.pkg.dev/PROJECT/AR_REPO/enterprise-agentic-rag:<git-sha>`, deploys, then polls `<url>/health`. It fails if `/health` doesn't return `{"status": "ok"}`.

**Finding the URL:**
- The run's **Summary** page shows "Deployed ✅" with the URL and image tag. The "Smoke test GET /health" step log also prints it.
- Or: `gcloud run services describe enterprise-agentic-rag --region asia-south1 --format='value(status.url)'`
- Or: Cloud Run console → service → URL at the top.

```bash
URL=$(gcloud run services describe enterprise-agentic-rag --region asia-south1 --format='value(status.url)')
curl "$URL/health"
curl -X POST "$URL/query" -H 'Content-Type: application/json' -d '{"q": "What is a Kubernetes CronJob?"}'
```

**Rollback:** every deploy is a new revision tagged with its commit SHA. In the console (Cloud Run → service → Revisions), route 100% of traffic to the previous revision. Or:
`gcloud run services update-traffic enterprise-agentic-rag --region asia-south1 --to-revisions=<REVISION>=100`

**Logs:** `gcloud run services logs read enterprise-agentic-rag --region asia-south1 --limit 100`, or Logfire if `LOGFIRE_TOKEN` is set.

---

## 5. Point the Streamlit UI at Cloud Run

The UI reads `BACKEND_URL` (default `http://localhost:8000`, see `ui/app.py`). Use the Cloud Run URL with no trailing slash and no path.

- **Locally:** `BACKEND_URL=https://enterprise-agentic-rag-xxxxx.a.run.app streamlit run ui/app.py`, or set it in `.env`.
- **Streamlit Community Cloud** (the live demo): app → **Settings → Secrets**, add a top-level entry and save (the app restarts):
  ```toml
  BACKEND_URL = "https://enterprise-agentic-rag-xxxxx.a.run.app"
  ```
  Top-level secrets are exposed as environment variables, so `os.getenv("BACKEND_URL")` picks it up.

The UI calls the API from Streamlit's servers, so the service must be public (`public = true`) unless you add ID-token auth to the UI. The first request after an idle period is slow (see the cold-start note below).

---

## 6. Cost and cold starts

The service is configured with min instances 0, max 2, 1 vCPU, 2 GiB RAM, a 300 s timeout, and request-based billing.

- **Cloud Run free tier** (per billing account, per month, in most regions including asia-south1): 180,000 vCPU-seconds, 360,000 GiB-seconds and 2 million requests. At 1 vCPU / 2 GiB the memory allowance runs out first: about 50 instance-hours of active request time per month. A demo with occasional traffic typically costs **$0**.
- **Min instances 0** means nothing runs and nothing is billed while idle. The cost: the first request after ~15 minutes of idleness hits a **cold start**. Cloud Run starts a container, imports LangChain / NeMo / Qdrant and initialises the guardrails, which takes roughly 10–30 s before the first answer. The ONNX models (FlashRank, BM25, NeMo's MiniLM) are baked into the image, so a cold start never downloads them. Setting `--min-instances=1` removes cold starts, but you pay for an always-on instance: roughly $30–60/month at 1 vCPU / 2 GiB with CPU always allocated, less with request-based idle pricing.
- **Max instances 2** caps compute cost under a traffic spike or abuse. It doesn't cap LLM spend: a public `/query` still calls Groq / Gemini through your keys. Keep provider-side budget limits and alerts on.
- **Artifact Registry:** 0.5 GB free, then about $0.10/GB-month. The image is ~1.5 GB (1.53 GB measured), so add a cleanup policy (section 2.2).
- **Secret Manager:** 6 active secret versions free, then $0.06 per version per month. 8 secrets cost about $0.12/month. Access operations at this volume are free.
- Set a **budget alert**: Billing → Budgets & alerts, for example $5/month with email alerts at 50/90/100%.

Prices change; confirm on the [Cloud Run pricing page](https://cloud.google.com/run/pricing).

---

## 7. Interview walkthrough

Use a question from `DATA/true_data/`. The planner classifies the request. Qdrant runs hybrid retrieval (dense + BM25, fused with RRF). FlashRank reranks the candidates. The response includes attributed evidence and a grounded verdict. Then show:
- `/health` on the Cloud Run URL and the Streamlit source panel;
- the **CI** run: lint, test (coverage in the log, `coverage.xml` artifact) and docker (image built and `/health` checked) as three parallel jobs;
- the **Deploy to Cloud Run** run: keyless WIF auth, SHA-tagged image, secrets from Secret Manager, post-deploy health gate.

---

## 8. Conversation memory, streaming and rate limits on Cloud Run

### 8.1 Why SQLite is not enough on Cloud Run

Locally the default `CHECKPOINTER=sqlite` keeps conversations in `./state/checkpoints.sqlite`, so they survive an API restart. On Cloud Run that file would live on the container's **in-memory filesystem**: it is wiped whenever an instance stops (scale to zero after ~15 idle minutes, every deploy), and each of the up to 2 instances has its **own** copy. A follow-up question routed to the other instance would see no history. Conversation memory on Cloud Run therefore needs a database every instance shares: `CHECKPOINTER=postgres`.

Without the steps below, the deploy uses `sqlite`: it works, but memory is per instance and temporary.

### 8.2 Create a Postgres database

Any Postgres 13+ works. Options, cheapest first:

| Option | Cost | Notes |
|---|---|---|
| [Neon](https://neon.tech) free tier | $0 | Serverless; suspends when idle (first query after idle takes ~1 s; the connection pool replaces dropped connections). Use the connection string with `sslmode=require`. |
| [Supabase](https://supabase.com) free tier | $0 | Use the **session pooler** or direct connection string. Prepared statements are disabled in the app, so the transaction pooler also works. |
| Cloud SQL for PostgreSQL | from ~$10/month (smallest shared-core instance) | Same GCP project; connect with the Cloud SQL connector / `--add-cloudsql-instances`, or a private IP + Serverless VPC connector. |

The app creates its tables on startup (`PostgresSaver.setup()`: `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`). It is idempotent, so every new instance can run it.

### 8.3 Store DATABASE_URL in Secret Manager and switch the backend

`DATABASE_URL` contains the database password, so it is a secret like the API keys (section 2.3), never a plain env var:

```bash
gcloud secrets create DATABASE_URL --replication-policy=automatic
read -rsp "DATABASE_URL: " value; echo
printf '%s' "$value" | gcloud secrets versions add DATABASE_URL --data-file=-
unset value

# The runtime service account must be able to read it (same as the other secrets).
gcloud secrets add-iam-policy-binding DATABASE_URL \
  --member="serviceAccount:$RUNTIME_SA_EMAIL" --role=roles/secretmanager.secretAccessor

# Tell the deploy workflow to use Postgres (a repository variable, not a secret).
gh variable set CHECKPOINTER --body postgres
```

With `vars.CHECKPOINTER == 'postgres'` the deploy workflow sets `CHECKPOINTER=postgres` and mounts `DATABASE_URL=DATABASE_URL:latest`. If the database is unreachable or the URL is wrong, startup fails, so the deploy's `/health` gate catches it instead of the first user.

Check it after a deploy: ask a question, wait for scale-to-zero (or redeploy), then ask a follow-up such as "what did I just ask?" on the same `thread_id`.

### 8.4 Streaming (`POST /query/stream`)

Same body as `/query`; the response is `text/event-stream` with `step`, `token`, `sources`, `done` (or `error`) events. Cloud Run streams HTTP/1.1 chunked responses without buffering, and the 300 s request timeout covers the longest self-correcting run. Try it:

```bash
curl -N -X POST "$URL/query/stream" -H 'Content-Type: application/json' \
  -d '{"q": "What is a Kubernetes CronJob?", "thread_id": "demo"}'
```

### 8.5 Rate limits

The deploy sets `TRUST_PROXY_HEADERS=true`: requests reach the container through Google's front end, so the TCP peer is Google's proxy, not the user, and every user would otherwise share one budget. The image starts uvicorn with `--no-proxy-headers`, so uvicorn never rewrites the client address itself and `TRUST_PROXY_HEADERS` is the only switch. The app uses the **rightmost** `X-Forwarded-For` entry, the address Google's front end appended; entries to its left are whatever the client sent and can be forged. If you later put an external HTTPS load balancer in front of Cloud Run, the rightmost entry becomes the load balancer's address: check the header in the logs and adjust `client_ip()` in `app/rate_limit.py`.

Defaults: `RATE_LIMIT_QUERY=20/minute` (shared by `/query` and `/query/stream`) and `RATE_LIMIT_UPLOAD=5/minute`, per client IP. Counters are in memory **per instance**, so with `--max-instances=2` a client can get up to 2× the limit; that is acceptable here and keeps Redis out of the stack. The limit protects the LLM budget per client; keep the provider-side budget alerts from section 6 as the global backstop.
