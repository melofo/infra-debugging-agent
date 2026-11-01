# Deployment

Two parts. **Local containers** were built and exercised for real (see "What was verified"). The **Cloud Run** part is a guide written from the design and the platform's documented behavior; it has **not been executed** because it needs a GCP project. Treat its commands as a checklist to try, and expect to adjust names and flags.

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
| `GEMINI_API_KEY`, `GEMINI_MODEL` | model access | secret / config |
| `JIRA_WEBHOOK_SECRET` | HMAC key for `/webhooks/jira` (fails closed when empty) | secret |
| `CORS_ORIGINS` | JSON array of the web UI origins | config |
| `WORKER_ENABLED` | run the worker inside the web process | config |
| `AGENT_VARIANT` | `v2` (default: `list_services` tool + "beyond deploys" prompt) or `v1` (previous behavior); sessions keep the variant they were created with | config |
| `AGENT_MAX_STEPS` | model calls allowed per turn (default 12; v2 searches more services) | config |
| `OTEL_EXPORTER`, `OTEL_ENDPOINT`, `OTEL_SAMPLE_RATIO` | tracing/metrics | config |
| `OTEL_EXPORTER_OTLP_HEADERS` | OTLP auth headers (standard variable, read by the exporter) | secret |
| `PORT` | listen port (Cloud Run sets it) | platform |

## 3. Cloud Run (guide, not executed)

Pieces: Artifact Registry (images), Cloud SQL for PostgreSQL with pgvector (database), Secret Manager (secrets), Cloud Run (web, and the frontend as a second service).

```bash
PROJECT=my-project REGION=europe-west1
gcloud config set project $PROJECT

# 1. Images
gcloud artifacts repositories create agent --repository-format=docker --location=$REGION
docker build -t $REGION-docker.pkg.dev/$PROJECT/agent/backend .
docker push   $REGION-docker.pkg.dev/$PROJECT/agent/backend

# 2. Database: Cloud SQL for PostgreSQL 15+ supports the pgvector extension (CREATE EXTENSION vector).
#    The application creates its tables itself at startup (idempotent, protected by an advisory lock).

# 3. Secrets (never put values on the command line in shell history you keep; use --data-file)
gcloud secrets create gemini-api-key --data-file=-          # paste, then Ctrl-D
gcloud secrets create database-url   --data-file=-
gcloud secrets create jira-webhook-secret --data-file=-

# 4. A dedicated service account with least privilege
gcloud iam service-accounts create agent-run
#    roles: roles/cloudsql.client, roles/secretmanager.secretAccessor (on those secrets only),
#           roles/cloudtrace.agent (if exporting traces)

# 5. Deploy the API
gcloud run deploy agent-api \
  --image $REGION-docker.pkg.dev/$PROJECT/agent/backend --region $REGION \
  --service-account agent-run@$PROJECT.iam.gserviceaccount.com \
  --add-cloudsql-instances $PROJECT:$REGION:agent-db \
  --set-env-vars ENVIRONMENT=production,WORKER_ENABLED=true,CORS_ORIGINS='["https://ui.example.com"]' \
  --set-secrets GEMINI_API_KEY=gemini-api-key:latest,DATABASE_URL=database-url:latest,JIRA_WEBHOOK_SECRET=jira-webhook-secret:latest \
  --min-instances 1 --no-cpu-throttling --timeout 3600 --port 8000 \
  --allow-unauthenticated    # the API enforces its own API keys; Jira cannot use Google IAM for the webhook
```

`DATABASE_URL` for Cloud SQL over the built-in connector is `postgresql://USER:PASSWORD@/agent?host=/cloudsql/PROJECT:REGION:INSTANCE`.

Decisions to understand before copying the flags:

- **Where the worker runs.** Cloud Run only allocates CPU while a request is being served (unless `--no-cpu-throttling`). An in-process worker therefore needs `--min-instances 1 --no-cpu-throttling`, which is billed continuously. The scalable alternative is `WORKER_ENABLED=false` on the API and the same image running `scripts/run_worker.py` as a separate always-on process (a Cloud Run worker pool if available to you, otherwise GKE or a VM). That script opens no port, so it cannot be a plain Cloud Run service.
- **Timeouts.** Streaming chat answers use SSE; `--timeout 3600` lets them outlive the default 5 minutes. A single investigation is bounded by `JOB_TIMEOUT_SECONDS`.
- **Several instances at once** is safe: schema creation takes an advisory lock, the job queue uses `FOR UPDATE SKIP LOCKED` with fencing, and sessions use a lease plus version check in Postgres.
- **The webhook is public by necessity**, so its protection is the HMAC signature (constant-time comparison, fail closed). Consider Cloud Armor rules limiting it to Jira's published addresses.
- **First admin key.** Do not run the key script as a Cloud Run job (the key would be printed into logs). Run it from a machine that reaches the database through the Cloud SQL Auth Proxy.
- **Frontend.** Build it with the API's public URL, `docker build --build-arg NEXT_PUBLIC_API_URL=https://agent-api-xxxx.a.run.app frontend`, deploy as a second service, and put its URL in the API's `CORS_ORIGINS`. The API key lives in the browser's `sessionStorage` (documented limitation); a cookie-based session would be stronger.
- **Tracing.** Point `OTEL_EXPORTER=otlp` at an OpenTelemetry Collector (a sidecar container) that exports to Cloud Trace; the app only speaks OTLP and stays vendor-neutral. Logs are JSON on stdout and already carry `trace_id`.
- **Model access.** This project uses a Gemini API key. On GCP the more usual route is Vertex AI with the service account's own identity (no key to store); that needs a new `LLMClient` implementation behind the existing interface and is not done.

## 4. Not done / honest gaps

- Nothing here was run on GCP. No load test was done, and there is no CI pipeline yet (a natural next step: build images, run the test suite, run the evaluation baselines on every change).
- The image has not been vulnerability-scanned (try `docker scout` or Trivy) and uses floating base-image tags (`python:3.11-slim`, `node:22-slim`); pin digests for reproducible builds.
- No database migration tool: tables are created idempotently from `app/schema.sql`. That is fine for adding tables; changing or dropping columns needs a real migration tool (Alembic).
