# Infra Debugging Agent

An AI agent that investigates production incidents. Given a Jira ticket, it queries logs, recent deployments and runbooks, compares root-cause hypotheses, and returns an **evidence-backed RCA report**.

> Status: work in progress (backend + agent core done; chat UI, Jira webhook, real integrations and deployment are planned).
> Version 1 is **read-only**: the agent can only query data, never change anything.

## What works today

- `POST /investigate` runs the agent on a simulated incident (`data/scenarios/bug-1042.json`) and returns the report, every tool call it made, and token/latency stats.
- Two interchangeable agent implementations, selectable with `mode`:
  - `loop` – a hand-written tool-calling loop (`app/agent/loop.py`): every step is visible and controllable.
  - `sdk` – the Google GenAI SDK's automatic function calling (`app/agent/sdk_afc.py`): much shorter, but the loop is a black box.
- **Sessions with follow-up questions** – the first message of a session runs the investigation; later messages continue the same agent loop with the full history (including earlier tool results), so follow-ups are answered from data already gathered.
  - `POST /sessions` → create a session
  - `POST /sessions/{id}/messages` → send a message, get the whole turn as JSON
  - `POST /sessions/{id}/messages/stream` → same, but streamed as Server-Sent Events (`step` events while the agent works, then one `turn` event)
  - `GET /sessions/{id}` → transcript
  - Safeguards: one message at a time per session (409 otherwise), failed turns are rolled back, turn and session count limits.
- `POST /chat` – plain LLM chat; `GET /health` – liveness check.

## Design notes

- **Evidence is enforced by code, not by prompt.** A hypothesis without at least one piece of evidence cannot be constructed (`app/schemas.py`); an invalid report is sent back to the model to fix.
- **The model only proposes; the backend executes.** Tools go through an allowlist registry with validated arguments and capped result sizes (`app/agent/tools.py`).
- **Provider-agnostic LLM layer** (`app/llm/base.py`); Gemini is the first implementation.
- **Ground truth is kept apart from the data the tools can read**, so the agent can later be evaluated fairly.
- Tests use scripted fake LLMs: no network, no cost, deterministic.

## Run locally

Requires Python 3.11+.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the project root (never commit it):

```
GEMINI_API_KEY=your-key-from-google-ai-studio
GEMINI_MODEL=a-gemini-model-id-available-to-you

# optional (defaults shown)
# LOG_LEVEL=INFO
# LLM_TIMEOUT_SECONDS=30
# LLM_MAX_ATTEMPTS=4      # attempts on 429/503, with exponential backoff
# AGENT_MAX_STEPS=8
```

Start the server and open the interactive docs at http://127.0.0.1:8000/docs:

```bash
uvicorn app.main:app --reload
```

Run the tests (no API key needed):

```bash
pytest
```

## Database

PostgreSQL (with the pgvector extension, used later for runbook retrieval) runs in Docker. It stores the investigation job queue used by the Jira webhook flow.

```bash
docker compose up -d db      # start (127.0.0.1:5432 only)
docker compose down          # stop; add -v to also delete the data
```

The app creates its tables at startup (`app/schema.sql`, idempotent) and still starts if the database is down: chat and investigations keep working, `GET /ready` reports 503, and job features are unavailable. `GET /health` is a liveness check and deliberately does not touch the database. Override the connection with `DATABASE_URL`; the default is a local, dev-only credential.

Database integration tests use a separate `agent_test` database, refuse to run against a non-local host, and are skipped automatically when PostgreSQL is not running.

## Jira webhook

`POST /webhooks/jira` receives Jira Cloud "issue created" events for Bugs and queues an investigation job (the job worker is added in a later step). It follows Jira's documented behaviour: the body is signed with HMAC-SHA256 (`X-Hub-Signature: sha256=<hex>`) and `X-Atlassian-Webhook-Identifier` identifies a delivery across retries.

- Set `JIRA_WEBHOOK_SECRET` to the secret configured on the Jira webhook. Without it the endpoint refuses everything (fail closed).
- The signature is verified over the raw body with a constant-time comparison before anything else is parsed or stored.
- Redelivered events are deduplicated; unrelated events are acknowledged with `200` and ignored.
- Status codes are chosen for Jira's retry rules: `202` queued, `200` duplicate/ignored, `401` bad signature, `400` malformed, `413` too large, `503` database unavailable (Jira retries those).

## Background worker

A worker runs inside the API process (started in the app lifespan) and executes queued jobs with the same investigation agent the chat uses, storing the report in the job's `result`. It processes one job at a time (model rate limits), retries transient failures with exponential backoff, fails permanently on bad data, enforces a per-job timeout, reclaims jobs whose worker disappeared, and on shutdown releases its current job back to the queue without using an attempt. Several instances can run side by side safely (`FOR UPDATE SKIP LOCKED`). It only starts when the LLM is configured; set `WORKER_ENABLED=false` to disable it. Tests disable it by default so they can never spend real model quota.

## Automatic investigations (results)

- `GET /jobs` – recent jobs with a preview (status, attempts, most likely cause and confidence); `GET /jobs/{id}` – full detail including the RCA report and the agent's steps.
- The UI has an "Automatic investigations" page (`/jobs`, polling every 3 seconds) and a detail page (`/jobs/<id>`) that stops polling once the job is finished.
- Error messages stored on jobs are user-facing: they never contain server paths or internal exception text (details go to the server log only).

## Chat UI

A Next.js front end lives in `frontend/` (see `frontend/README.md`). With the backend running:

```bash
cd frontend && npm install && npm run dev   # http://localhost:3000
```

The backend allows browser calls from `http://localhost:3000` by default (CORS allowlist, configurable with `CORS_ORIGINS`).

## Known limitations

- No authentication or rate limiting yet (including on `/jobs`, which exposes investigation results): run it locally only.
- Data sources are simulated JSON files; real Jira / log / Git connectors are planned.
- Free-tier Gemini quotas (about 5 requests/minute) can throttle multi-step investigations.
