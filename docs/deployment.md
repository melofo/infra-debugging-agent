# Deployment

Two parts, both executed. **Local containers** were built and exercised for real (see "What was verified"). The **Google Cloud** part (section 3) was executed in a dedicated project: Cloud Run (API, worker pool, frontend), Cloud SQL with pgvector, Secret Manager, Artifact Registry and Cloud Build, with Gemini reached through the service account (no API key). A self-hosted model on a Cloud Run GPU is **not** done yet (section 3.7).

## 1. Run everything in containers locally

```bash
docker compose --profile app up --build -d      # db + backend + worker + frontend
open http://localhost:3000                      # UI; API on http://localhost:8000
docker compose --profile app down               # add -v to also delete the database volume
```

Plain `docker compose up -d db` still starts only the database (for day-to-day development with the backend and frontend running in your terminals).

Secrets come from `.env` in the project root (read by the containers at start, never copied into an image: see `.dockerignore`). Create an API key for the UI:

```bash
docker compose exec backend python scripts/manage_api_keys.py create --name me --role admin
```

Ports and names are overridable (`DB_PORT`, `BACKEND_PORT`, `FRONTEND_PORT`, `POSTGRES_PASSWORD`), and `-p <name>` gives a second, fully isolated copy (own containers, own database volume).

### Shape of the images

| | backend | frontend |
|---|---|---|
| base | `python:3.11-slim`, two stages | `node:22-slim`, three stages, Next.js `standalone` output |
| size | ~300 MB | ~310 MB |
| user | `app` (uid 10001), code is read-only to it | `node` |
| dependencies | `requirements-prod.txt` (no test tools) | `npm ci` from the lockfile |
| stop signal | `exec uvicorn` makes it PID 1, so SIGTERM triggers the graceful shutdown | Node default (exits 143, nothing to drain) |
| health | `HEALTHCHECK` on `/health` | `HEALTHCHECK` on `/` |

The same backend image runs the web service and the worker (`python scripts/run_worker.py`). `NEXT_PUBLIC_API_URL` is a **build argument** of the frontend image (Next.js bakes `NEXT_PUBLIC_` variables into the browser bundle), so one frontend image is tied to one backend URL.

### What was verified (on this machine, in real containers)

- Images build from a clean checkout; the backend image has no `.env`, no tests, no pytest, runs as uid 10001 and cannot write to `/app`; all 13 knowledge documents and 37 scenario files are inside.
- Stack comes up healthy (`db` first, then `backend`, then `frontend`); `/health` and `/ready` answer; `database ready` is logged.
- Auth is enforced (401 without a key, 200 with one created by the script inside the container); CORS preflight from the UI origin is allowed including the `Authorization` header.
- A chat session survives `docker compose restart backend` (it lives in Postgres).
- With `WORKER_ENABLED=false` on the web service and a separate `worker` container, a queued job was processed by the worker container only. The worker has no port, so its health is a heartbeat file written by a task that shares the event loop with the job loop (stuck loop = stale heartbeat = unhealthy; a long investigation awaiting the model keeps beating).
- `docker compose stop`: worker and backend exit with code 0 in about half a second (graceful shutdown works), frontend exits 143.

Two real bugs were found by doing this and fixed: (1) schema creation raced when several instances started at once (35 errors in 8-way tests, and the knowledge step silently reported "pgvector unavailable"); it now takes a Postgres advisory lock. (2) The frontend lockfile was generated on macOS and lacked two optional packages, so `npm ci` failed on Linux; the lockfile was regenerated on Linux.

## 2. Production configuration

Set `ENVIRONMENT=production`. The application then **refuses to start** unless `AUTH_REQUIRED=true`, `SESSION_STORE=postgres` and `CORS_ORIGINS` lists real (non-localhost) origins, so forgetting a safe setting becomes a startup error instead of a quiet hole.

| Variable | Purpose | Where it should come from |
|---|---|---|
| `DATABASE_URL` | Postgres connection (needs the `vector` extension for knowledge search) | secret |
| `LLM_PROVIDER` | `gemini-api` (API key), `vertex` (Gemini on Google Cloud, service-account identity, no key; the platform is now called Gemini Enterprise Agent Platform) or `openai-compatible` (Ollama, vLLM, OpenAI) | config |
| `GEMINI_API_KEY` | model access when `LLM_PROVIDER=gemini-api` | secret |
| `GEMINI_MODEL` | model name for both Gemini channels | config |
| `GCP_PROJECT`, `GCP_LOCATION` | for `vertex`; a model may exist only in the `global` location (the one used here returned 404 in `us-central1`) | config |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_AUTH` | for `openai-compatible`; `LLM_AUTH=google-id-token` sends a service-account identity token to a private Cloud Run model server | config / secret |
| `JIRA_WEBHOOK_SECRET` | HMAC key for `/webhooks/jira` (fails closed when empty) | secret |
| `CORS_ORIGINS` | JSON array of the web UI origins | config |
| `WORKER_ENABLED` | run the worker inside the web process | config |
| `AGENT_VARIANT` | `v2` (default: `list_services` tool + "beyond deploys" prompt) or `v1` (previous behavior); sessions keep the variant they were created with | config |
| `AGENT_MAX_STEPS` | model calls allowed per turn (default 12; v2 searches more services) | config |
| `OTEL_EXPORTER`, `OTEL_ENDPOINT`, `OTEL_SAMPLE_RATIO` | tracing/metrics | config |
| `OTEL_EXPORTER_OTLP_HEADERS` | OTLP auth headers (standard variable, read by the exporter) | secret |
| `PORT` | listen port (Cloud Run sets it) | platform |

## 3. Google Cloud (executed)

A dedicated project, so that deleting the project is a complete clean-up. Region `us-central1` for the services; the Gemini model itself is called in the `global` location.

### 3.1 What runs where

| Piece | Service | Notes |
|---|---|---|
| API | Cloud Run **service** | `WORKER_ENABLED=false`, scales 0-2, public URL (the API enforces its own API keys and the webhook HMAC) |
| Worker | Cloud Run **worker pool** | the same image with the start command overridden to `python scripts/run_worker.py`; no HTTP port, so it cannot be a service; one always-on instance (it polls the queue) |
| Frontend | Cloud Run **service** | its own image; `NEXT_PUBLIC_API_URL` is a build argument |
| Database | Cloud SQL for PostgreSQL 17 | shared-core tier, single zone, no backups (a demo), public IP with **no authorized networks**: Cloud Run connects through the built-in connector, an operator through the Cloud SQL Auth Proxy |
| Knowledge base | tables in that same database | `knowledge_documents` and `knowledge_chunks` (`vector(768)`, HNSW cosine index), filled by a one-off Cloud Run **job** that runs `scripts/ingest_knowledge.py` with the service account (Vertex embeddings) |
| Secrets | Secret Manager | database password, database URL, webhook secret, the first admin API key; the runtime service account can read only the ones it needs |
| Identity | one service account | roles: Cloud SQL Client, Vertex AI User (Gemini and embeddings); no key file exists |
| Images | Artifact Registry | built by Cloud Build from the *committed* files (`git archive`), so no local `.env` or result files can leak in; the build must run remotely because a Mac produces arm64 images and Cloud Run needs amd64 |

### 3.2 The sequence that was run (variables, not real values)

```bash
# project and billing (a new project; never link billing to a project that holds an API key you rely on)
gcloud projects create "$PROJECT" && gcloud billing projects link "$PROJECT" --billing-account="$BILLING"
gcloud services enable run.googleapis.com sqladmin.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com cloudbuild.googleapis.com aiplatform.googleapis.com --project="$PROJECT"
gcloud artifacts repositories create agent --repository-format=docker --location=us-central1

# database: a random password goes straight into Secret Manager (never echoed)
gcloud sql instances create agent-db --database-version=POSTGRES_17 --edition=ENTERPRISE --tier=db-g1-small \
  --region=us-central1 --storage-size=10GB --availability-type=zonal --no-backup
gcloud sql databases create agent --instance=agent-db
gcloud sql users create agent --instance=agent-db --password="$PW"     # members get CREATE EXTENSION rights

# images (remote build from the committed files)
gcloud builds submit "$EXPORT_DIR" --tag us-central1-docker.pkg.dev/$PROJECT/agent/backend:v1
# frontend: a build step passes --build-arg NEXT_PUBLIC_API_URL=<the API URL>

# API: Gemini through the service account, secrets from Secret Manager
gcloud run deploy agent-api --image ... --service-account "$SA" --add-cloudsql-instances "$CONN" \
  --set-env-vars "^@^ENVIRONMENT=production@WORKER_ENABLED=false@LLM_PROVIDER=vertex@GCP_PROJECT=$PROJECT@GCP_LOCATION=global@GEMINI_MODEL=...@CORS_ORIGINS=[...]" \
  --set-secrets "DATABASE_URL=database-url:latest,JIRA_WEBHOOK_SECRET=jira-webhook-secret:latest" \
  --min-instances 0 --max-instances 2 --timeout 3600 --port 8000 --allow-unauthenticated

# worker: same image, different start command, always on
gcloud run worker-pools deploy agent-worker --image ... --command python --args scripts/run_worker.py --instances 1 ...

# knowledge base: a one-off job; first admin key: from a laptop through the Cloud SQL Auth Proxy (stored in Secret Manager)
gcloud run jobs create ingest-knowledge --image ... --command python --args scripts/ingest_knowledge.py ... && gcloud run jobs execute ingest-knowledge --wait

# frontend, then allow its origin(s) in the API
gcloud run deploy agent-frontend --image .../frontend:v1 --port 3000 --allow-unauthenticated
gcloud run services update agent-api --update-env-vars "^@^CORS_ORIGINS=[\"https://frontend-url-1\",\"https://frontend-url-2\"]"
```

### 3.3 Where it differs from the earlier design

- The worker is a **separate worker pool**, not an in-process thread, so the API can scale to zero (the old design needed `--min-instances 1 --no-cpu-throttling` on the API).
- Model access is the service account's own identity (`LLM_PROVIDER=vertex`), so there is **no model API key** anywhere in the deployment.
- Cloud Run gives a service two valid URLs; the browser's origin is whichever one is opened, so both are listed in `CORS_ORIGINS`.
- Knowledge ingestion runs as a job with the runtime service account instead of from a laptop (it needs Google credentials for the embeddings); the first admin key is created from a laptop through the proxy so that it is never written to a job's logs.

### 3.4 What was verified on the deployed system

- Health check, and a protected endpoint without a key returns 401.
- All webhook behaviours against the public URL, each as expected: bad signature, tampered body, missing signature and wrong algorithm are rejected with 401; a Task, an update event and a ticket without data are ignored with 200; a redelivery is recognised as a duplicate; a correct signed event is queued with 202.
- Startup created the eight tables and the `vector` extension; the knowledge job embedded 13 documents into 49 chunks.
- End to end through the real path (signed webhook, queue, worker, Gemini, report), **all 36 evaluation scenarios were answered correctly in one run each** (30 of 30 development, 6 of 6 held-out, no errors), with `scripts/eval_deployed.py`: 7.0 model calls and about 16,200 input / 480 output tokens per investigation, wall time p50 8.1 s and p95 10.9 s including queueing. Model: a small, fast Gemini model. Limits: one run per scenario, synthetic data, and the development scorer was tuned after seeing outputs, so the held-out 6 of 6 is the figure to trust.
- The chat UI (streamed steps, knowledge search citing runbook and past-incident ids) was used by hand in a browser.

### 3.5 Problems met while doing it

- In zsh, `$PROJECT:us-central1:db` applies the `:u` modifier (upper-case) to the variable, which corrupted the Cloud SQL connection name inside a secret. Use `${PROJECT}`.
- The Gemini model used here answers in the `global` location and returns 404 in `us-central1`; the embedding model works in both.
- The `gcloud run worker-pools` commands need the `grpc` Python module; a gcloud installed with a system Python needs it installed in that environment.
- `scripts/simulate_jira.py --wait` crashed against a protected backend (it polled the job without an API key); it now sends `AGENT_API_KEY` on the polling request only.

### 3.6 Cost and clean-up

Billed continuously while it runs (estimates from list prices, not invoices): the Cloud SQL instance (about $1 a day) and the always-on worker (about $1.70 a day); the API and frontend scale to zero. A budget alert does **not** cap spending. Clean-up is deleting the project (`gcloud projects delete`), which removes everything in it; do not link billing to a project that holds an API key you still rely on.

### 3.7 Not done

- A self-hosted open model on a Cloud Run GPU (planned: vLLM, private service, identity-token auth; the adapter for it exists and is tested, see section 5.6 of the evaluation report for the local-model results).
- OpenTelemetry export to Cloud Trace (logs are JSON on stdout with `trace_id`; no collector is deployed).
- A load test, Cloud Armor in front of the webhook, a CI deploy pipeline, high availability and backups for the database, and a migration tool.

### 3.8 Planned, not executed: a self-hosted model on a GPU

What hosting an open-weight model involves:

> A model repository has three essential parts. **Weights** (`*.safetensors`) are the learned parameters, the "brain", and by far the biggest part (about 52 GB in bf16 for Gemma 4 26B). **`config.json`** is the architecture: layer count, hidden size, attention heads, context length, so the runtime knows how to build the network the weights fit into. **`tokenizer.json`** (with the chat template) is the language encoding: how text becomes token ids and back, which must match training exactly or the model gets garbage. An inference server such as vLLM loads all three, and serves them behind an API.

The tools: Hugging Face is where the three parts are published; Ollama runs a model locally from a quantized single-file **GGUF** package (weights, tokenizer rules and metadata together; this is what was used on a laptop, about 16 GB for the 26B model); vLLM is a production inference server that reads the Hugging Face format directly (bf16 weights) and offers batching, prefix caching and a tool-call parser. The two use different artifacts, so a local result and a cloud result are not the same model at the same precision.

Why an OpenAI-compatible adapter for open models: open weights have no API of their own; the serving software provides it, and Ollama and vLLM both implement the OpenAI chat-completions shape, so one adapter (`app/llm/openai_compat.py`) works for a local server, a vLLM service and OpenAI itself, with only `LLM_BASE_URL` and `LLM_MODEL` changing. Nothing is sent to OpenAI.

Planned configuration (not run): the smallest Gemma 4 model (`google/gemma-4-E2B-it`) with vLLM on a Cloud Run **L4** GPU, using Google's published `pytorch-vllm-serve:gemma4` image, a **private** service (`--no-allow-unauthenticated`, so requests without an identity token never reach the container or wake the GPU), zonal redundancy off, at most one instance, scaling to zero, called by the worker with `LLM_AUTH=google-id-token`. The goal is to measure cold start, tokens per second and cost per request, not model quality: a model this small is expected to do poorly on the tool-calling task. Estimated cost for one test session is a few dollars.

## 4. Not done / honest gaps

- No load test was done. CI exists (see the architecture document) but does not deploy.
- The image has not been vulnerability-scanned (try `docker scout` or Trivy) and uses floating base-image tags (`python:3.11-slim`, `node:22-slim`); pin digests for reproducible builds.
- No database migration tool: tables are created idempotently from `app/schema.sql`. That is fine for adding tables; changing or dropping columns needs a real migration tool (Alembic).
- The data sources are simulated, so the deployed system investigates simulated incidents; real connectors are the next step.
