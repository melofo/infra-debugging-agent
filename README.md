# Infra Debugging Agent

![CI](https://github.com/melofo/infra-debugging-agent/actions/workflows/ci.yml/badge.svg)

An AI agent that investigates production incidents. Given a Jira ticket (or a chat message), it queries logs, recent deployments, runbooks and past incidents, compares root-cause hypotheses and returns an **evidence-backed RCA report**, where every cited piece of evidence is verified against what the tools actually returned. It is **read-only**: it can only *propose* a Jira comment, which a human must approve.

Portfolio project built to practice production-grade LLM agent engineering: reliability, security, evaluation and operations, not just a demo. Data sources are **simulated** (JSON scenarios); everything around the agent is real.

## At a glance

| Area | What is there |
|---|---|
| Agent | Hand-written tool-calling loop (plus an SDK variant for comparison), Gemini behind a provider-agnostic interface, tools exposed and consumed over **MCP** (stdio and authenticated Streamable HTTP) |
| Trust | **Evidence grounding** checked in code (a reference must appear verbatim in a successful tool result of the right kind), strict tool allowlist, MCP server treated as untrusted |
| Safety | API keys with roles (hashed, uniform 401, fail closed), HMAC-signed Jira webhook, **human approval** for write actions with content-hash binding, append-only audit log |
| Backend | FastAPI, PostgreSQL + pgvector, durable job queue (`SKIP LOCKED`, fencing, backoff), Postgres-backed chat sessions with lease + versioning, SSE streaming |
| Retrieval | Chunked runbooks and past incidents, vector search (HNSW cosine), retrieval evaluation against BM25 and hybrid |
| Evaluation | 36 generated incident scenarios, deterministic scorer, no-LLM baselines, held-out set, paired comparison with a sign test |
| Operations | OpenTelemetry traces and metrics, Docker (non-root, graceful shutdown), compose stack, CI on GitHub Actions, container smoke test |
| Frontend | Next.js chat UI with live agent steps, automatic-investigation pages, approvals page |

```
Jira webhook --HMAC--> job queue (Postgres) --> worker --+
Chat UI --API key--> FastAPI --------------------------- +--> agent loop (Gemini)
                                                              |  tools: tickets, logs, deploys, runbooks,
                                                              |         knowledge search (pgvector), list_services
                                                              |  every report: evidence grounding check
                                                              v
                          RCA report --> proposed Jira comment --> human approval --> posted (audit log)
```

## Results

Measured, with their limits stated. Details in *End-to-end evaluation* below.

| What | Result |
|---|---|
| Reference lines on 30 dev scenarios (no model) | oracle 100%, "blame the latest deploy" 20%, "copy the runbook" 0%, "echo the ticket" 0% |
| Gemini, original agent (v1), dev set | 24/30 Top-1 (80%); 15/15 when a deploy caused it, 9/15 otherwise |
| Gemini, improved agent (v2), dev set | 30/30, but the scorer was adjusted after seeing outputs, so treat it as optimistic |
| Held-out set (6 unseen incident types, 3 runs each) | v1 0/18, v2 18/18 (paired sign test p = 0.031); cause confirmed: v2 searches the other service's logs in 18/18 vs 0/18 |
| Cost of v2 | about +21% input tokens per investigation, latency about the same |
| Retrieval (35 labeled queries) | vector search hit@3 1.00 vs BM25 0.80; on queries sharing almost no words with their answer 1.00 vs 0.25 |
| Tests | 701 backend and 45 frontend tests, mutation-checked; CI green |

Caveats: the scenarios are synthetic and come from one generator, 30 cases give wide intervals, keyword scoring is coarse, and the step-budget control run failed because of a Gemini quota error. Numbers are good for comparing versions of this agent, not for claiming real-world accuracy.

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
# AGENT_VARIANT=v2        # v2 = list_services tool + "beyond deploys" prompt; v1 = previous behavior
# AGENT_MAX_STEPS=12
# AUTH_REQUIRED=true      # set false only for purely local experiments (then no API key is needed)
```

`scripts/check_env.py` verifies the `.env` is read correctly without printing any secret.

**First run:** the API requires an API key (see *API authentication*). Either set `AUTH_REQUIRED=false` in `.env` for a quick look, or start the database and create a key:

```bash
docker compose up -d db
.venv/bin/python scripts/manage_api_keys.py create --name me --role admin    # shown once; paste it in the web UI's top bar
```

Start the server and open the interactive docs at http://127.0.0.1:8000/docs:

```bash
uvicorn app.main:app --reload
```

Run the tests (no API key needed, and the test environment never has one):

```bash
pytest                 # database tests are skipped if PostgreSQL is not running
docker compose up -d db && REQUIRE_DB=1 pytest    # the full suite, as CI runs it
```

Without any keys or Docker you can still run the free evaluation baselines: `python scripts/eval_agent.py --llm oracle`.

## Database

PostgreSQL (with the pgvector extension, used for runbook retrieval) runs in Docker. It stores the investigation job queue, chat sessions, API keys, write-action proposals and the audit log.

```bash
docker compose up -d db      # start (127.0.0.1:5432 only)
docker compose down          # stop; add -v to also delete the data
```

The app creates its tables at startup (`app/schema.sql`, idempotent) and still starts if the database is down: chat and investigations keep working, `GET /ready` reports 503, and job features are unavailable. `GET /health` is a liveness check and deliberately does not touch the database. Override the connection with `DATABASE_URL`; the default is a local, dev-only credential.

Database integration tests use a separate `agent_test` database, refuse to run against a non-local host, and are skipped automatically when PostgreSQL is not running.

## Jira webhook

`POST /webhooks/jira` receives Jira Cloud "issue created" events for Bugs and queues an investigation job. It follows Jira's documented behaviour: the body is signed with HMAC-SHA256 (`X-Hub-Signature: sha256=<hex>`) and `X-Atlassian-Webhook-Identifier` identifies a delivery across retries.

- Set `JIRA_WEBHOOK_SECRET` to the secret configured on the Jira webhook. Without it the endpoint refuses everything (fail closed).
- The signature is verified over the raw body with a constant-time comparison before anything else is parsed or stored.
- Redelivered events are deduplicated; unrelated events are acknowledged with `200` and ignored.
- Status codes are chosen for Jira's retry rules: `202` queued, `200` duplicate/ignored, `401` bad signature, `400` malformed, `413` too large, `503` database unavailable (Jira retries those).

## Background worker

A worker runs inside the API process (started in the app lifespan) and executes queued jobs with the same investigation agent the chat uses, storing the report in the job's `result`. It processes one job at a time (model rate limits), retries transient failures with exponential backoff, fails permanently on bad data, enforces a per-job timeout, reclaims jobs whose worker disappeared, and on shutdown releases its current job back to the queue without using an attempt. Several instances can run side by side safely (`FOR UPDATE SKIP LOCKED`). It only starts when the LLM is configured; set `WORKER_ENABLED=false` to disable it. Tests disable it by default so they can never spend real model quota.

## Try the whole Jira flow without Jira

`scripts/simulate_jira.py` plays Jira: it builds Jira-format events, signs them with `JIRA_WEBHOOK_SECRET` and posts them to the webhook (the secret is never printed). With the backend and database running:

```bash
docker compose up -d db
.venv/bin/uvicorn app.main:app --reload                      # terminal 1
.venv/bin/python scripts/simulate_jira.py created --wait     # terminal 2: queue a Bug and wait for the report
```

Then open http://localhost:3000/jobs to watch the job go Queued, Running, Done. Other scenarios exercise the security and retry rules: `duplicate` (redelivery is deduplicated), `bad-signature`, `tampered`, `no-signature`, `wrong-method` (all rejected with 401), `non-bug`, `updated`, `unknown-ticket` (acknowledged and ignored). Each prints the status code it expects and exits non-zero if the server disagrees.

`scripts/check_env.py` verifies that `.env` is read correctly without printing any secret.

## Automatic investigations (results)

- `GET /jobs` – recent jobs with a preview (status, attempts, most likely cause and confidence); `GET /jobs/{id}` – full detail including the RCA report and the agent's steps.
- The UI has an "Automatic investigations" page (`/jobs`, polling every 3 seconds) and a detail page (`/jobs/<id>`) that stops polling once the job is finished.
- Error messages stored on jobs are user-facing: they never contain server paths or internal exception text (details go to the server log only).

## Knowledge base (RAG)

`data/knowledge/` holds runbooks and past incident write-ups (Markdown with YAML front matter). They are split into heading-based chunks (`app/rag/chunking.py`), turned into 768-dimension vectors (`app/rag/embedder.py`: Gemini embeddings, or an offline hash embedder for tests and key-less development via `EMBEDDING_PROVIDER=hash`) and stored in PostgreSQL with pgvector (`app/schema_knowledge.sql`, HNSW cosine index). Load it with `scripts/ingest_knowledge.py` (idempotent: unchanged documents are skipped and never re-embedded; `--dry-run` shows the plan; `--embedder hash` works offline). Ingestion computes all vectors first and then switches the database in one transaction, so a failure never leaves a half-updated knowledge base. Search it with `scripts/search_knowledge.py "your question"`: it embeds the question, finds the nearest chunks by cosine distance (HNSW index) and applies optional filters (document type, service). The agent uses it through the `search_knowledge` tool (runbooks and past incidents, cited by document id); when the knowledge base is not ingested or pgvector is unavailable it falls back to the keyword runbook search, and the next investigation picks the knowledge base up automatically after ingestion. Without the pgvector extension the app still starts; only semantic search is unavailable.

### Evaluating retrieval

`scripts/eval_retrieval.py` compares retrieval strategies (random floor, BM25 keyword search, vector search over whole documents / chunks of 1000 / chunks of 400, hybrid BM25 + vector with rank fusion) on 35 labeled queries (`data/eval/retrieval_queries.json`) and reports hit@k, recall@k and MRR, overall and per query category. It runs in memory (no database); a test checks that its ranking matches the database search. The `disjoint` category contains questions that deliberately share almost no words with their answers, enforced by a test. Use `--embedder hash` for an offline plumbing check (its numbers say nothing about semantic quality) and the default Gemini embedder for real results.

## Evidence grounding

A report is only accepted if every piece of evidence can be traced to data the tools actually returned (`app/agent/grounding.py`). The model is told to copy each `reference` verbatim; the code then checks it:

- it must appear (ignoring case and extra whitespace) inside one string returned by a **successful** tool call in this conversation, not in what the user or the model said, and not stitched across fields;
- the evidence type decides which tools may supply it: `jira` from `get_ticket`, `log` from `search_logs`, `git` from `get_recent_deploys`, `runbook` from the runbook/knowledge search, `incident` from `search_knowledge`. `metric` and `trace` have no tool, so they are always rejected;
- references must not be trivially short (log references need 20+ characters).

A rejected report goes back to the model with the exact evidence that failed; after two rejections in one turn the turn fails (and is rolled back) rather than delivering an untraceable conclusion. Both the hand-written loop and the SDK variant enforce it, and it works the same through MCP. What it cannot catch: a real quote used to support a wrong conclusion. That is measured by the end-to-end evaluation, not by this check.

## End-to-end evaluation

`scripts/eval_agent.py` runs the whole agent (tools, grounding check, report) on 30 incident scenarios (`data/scenarios/bug-2001..2030`, generated by `scripts/build_eval_scenarios.py` from `app/evals/scenarios.py`) and reports Top-1/Top-3 accuracy with 95% Wilson intervals, by difficulty, by category and by "was there a culprit deploy", plus whether the key evidence was cited, how often reports were rejected for unverifiable evidence, calibration, latency, tokens and (if you pass prices) cost.

The scenarios are built to separate good from bad agents: 10 root-cause archetypes x easy/medium/hard; half have **no culprit deploy** (expired certificate, rotated secret, traffic spike, slow dependency, feature flag); hard ones have an innocent deploy shortly before the incident; the key evidence for some is in another service's logs; tickets only describe symptoms; runbooks list several candidate causes. Scoring is deterministic: a hypothesis is correct if it names the incident-specific facts (`must_have`, e.g. the config key or flag name) and matches at least two concept groups. Tests check that the ticket alone, the runbook text alone, and other categories' answers never score.

Reference lines that cost nothing and run through the same loop and grounding checks: `--llm oracle` (reads the answer; must be 100%), `--llm latest_deploy` (about 20%), `--llm runbook_copy` and `--llm ticket_echo` (0%). Judge a real model against those, not in isolation.

```bash
.venv/bin/python scripts/eval_agent.py --llm oracle
.venv/bin/python scripts/eval_agent.py --llm gemini --limit 3                  # costs quota: try a few first
.venv/bin/python scripts/eval_agent.py --llm gemini --sleep 15 --out eval_results/run1.jsonl
.venv/bin/python scripts/eval_agent.py --llm gemini --repeat 3                 # stability across runs
```

Results are appended per case (an interrupted run resumes with the same `--out`) and keep the full report, the steps taken and which services' logs were searched. `--rescore FILE` re-scores saved results with the current scoring rule without calling the model (exact for files that contain the full report, approximate for older ones). The harness also reports whether the agent looked in the service that holds the key evidence when that was not the ticket's own service.

First real run (Gemini, 30 cases): 22/30 Top-1 with the original scorer. Auditing every miss showed 4 scoring false negatives (substantively correct answers that missed a keyword wording), 3 genuine misses (feature-flag incidents: the model stopped at the proximate failure instead of finding the flag change in `config-service`) and 1 step-budget failure. The scorer was then adjusted with a documented rule and guard tests (incident-specific facts must come from the incident's logs and appear in no ticket or runbook; baselines must stay at 0-20%). Because the scorer was tuned after seeing that run, treat the earlier numbers as optimistic and re-run for a clean figure. Errors count as wrong answers, never dropped from the denominator. Caveats: 30 cases means wide intervals (one case = 3.3 points); the scenarios are synthetic and keyword scoring is coarse, so treat numbers as relative, not absolute.

### Result: v2 is now the default agent configuration

Outcome of the experiment below (Gemini, same scenarios, both runs re-scored with the same rule): development set 24/30 -> 30/30 (paired sign test p=0.031; 6 cases newly correct, none newly wrong), held-out set 0/18 -> 18/18 (p=0.031), and the mechanism checks out: when the key evidence was logged by another service, v1 searched that service in 2/6 (dev) and 0/18 (holdout) cases, v2 in 6/6 and 18/18. Cost: about +8% model calls and +21% input tokens per investigation, latency about the same. So `AGENT_VARIANT=v2` (list_services tool + the "beyond deploys" prompt) and `AGENT_MAX_STEPS=12` are the defaults everywhere an agent is built: background worker, `/investigate`, chat sessions, and the MCP server; set `AGENT_VARIANT=v1` to go back. A chat session records the variant it was created with and keeps that tool set for its whole life, even across restarts or a changed setting.

Caveats worth saying out loud: the scenarios are synthetic and come from one generator, the scorer was adjusted twice after seeing outputs (the held-out scorer was not touched), 6 held-out scenarios is small, and the "v1 with 12 steps" control run failed (Gemini free-tier quota, 429), so the step-budget effect is only argued from v1 never hitting its limit on the held-out set. `compare_evals.py` now warns when a run is mostly errors.

### Evaluation-driven improvement (controlled experiments)

The first real runs showed Gemini is strong at "which deploy broke it" (15/15) but weak at changes that are not deploys and are logged by another service (feature flags, rotated secrets): it never searched `config-service` or `secrets-manager`, probably because it did not know they exist. The experiment: `--agent v2` adds a read-only `list_services` tool and a generic "beyond deploys" paragraph to the prompt; `--agent v1` is the current behavior; `--max-steps` is controlled separately because the extra searching needs a larger step budget.

To avoid fooling ourselves: 6 **held-out** scenarios (`--split holdout`: autoscaler limit lowered, DNS record changed, network policy blocking egress) have incident types that do not appear in the 30 development scenarios and are not named in the prompt (a test enforces that). An improvement only counts if it also helps there. `scripts/compare_evals.py A.jsonl B.jsonl` re-scores both files with the current rule and compares **the same scenarios pairwise** (newly correct vs newly wrong, with an exact sign test), because two accuracy percentages from 30 noisy runs say little: running the same v1 agent twice differed by one case.

```bash
.venv/bin/python scripts/eval_agent.py --llm gemini --agent v2 --max-steps 12 --sleep 15 --out eval_results/v2-dev.jsonl
.venv/bin/python scripts/eval_agent.py --llm gemini --agent v1 --max-steps 12 --sleep 15 --out eval_results/v1-12-dev.jsonl   # ablation: is it just the larger step budget?
.venv/bin/python scripts/eval_agent.py --llm gemini --split holdout --agent v1 --repeat 3 --sleep 15 --out eval_results/v1-holdout.jsonl
.venv/bin/python scripts/eval_agent.py --llm gemini --split holdout --agent v2 --max-steps 12 --repeat 3 --sleep 15 --out eval_results/v2-holdout.jsonl
.venv/bin/python scripts/compare_evals.py eval_results/run2.jsonl eval_results/v2-dev.jsonl
```

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request, with no secrets and read-only permissions: **backend** (the test suite against a real pgvector Postgres service, then the free evaluation baselines in the job summary), **frontend** (`npm ci`, lint, type check, tests, production build), **docker** (builds both images, starts the whole stack and runs `scripts/smoke_test_stack.py`, the same script you can run locally; it checks health, auth, CORS, session persistence across a restart, the separate worker (including its heartbeat-based health), the frontend bundle, non-root, no `.env` in the image, and graceful shutdown) and a non-blocking dependency **audit**. A missing CI database fails the tests (`REQUIRE_DB=1`) instead of silently skipping them. Static tests pin the workflow's safety properties (least privilege, official actions only, no `pull_request_target`, image and runtime versions equal to the Dockerfiles').

Honest status: the first real run on GitHub went red in two places that had passed locally, both because something existed only in my working directory: a test that silently relied on the real model key in my local `.env` (a request returned 503 before the ownership check when no key was configured), and a type check that needs Next's generated route types, which a fresh checkout does not have. Fixes: tests now run with the model credentials blanked (so a local run equals CI and no test can call the real model), and `npm run typecheck` runs `next typegen` first. The second run was green on all four jobs. Later, after pinning the runner image, the docker job failed because the newer Compose on that image refuses `up --wait` when a service has no healthcheck: the worker's check had been disabled. The log pointed at a real gap (a stuck worker would have looked "up" forever), so the worker now writes a heartbeat from a separate task and its container healthcheck reads it (`app/heartbeat.py`). To catch this class of problem before pushing, validate in a clean export of the committed files (no `.env`, no generated files). Actions are pinned to major version tags, not commit SHAs; pinning SHAs is the stricter supply-chain choice.

## Docker and deployment

`docker compose --profile app up --build -d` runs the whole system in containers (database, web API, a separate worker process, frontend); plain `docker compose up -d db` still starts only the database for development. Images are multi-stage, run as non-root, carry no secrets or test tooling, and shut down gracefully on SIGTERM. `ENVIRONMENT=production` makes the app refuse unsafe defaults at startup. See `docs/deployment.md` for what was verified locally, the production settings, and a Cloud Run guide (written but not executed: it needs a GCP project).

## Observability (OpenTelemetry)

Traces and metrics follow the standard OpenTelemetry model, so the same instrumentation can feed Cloud Trace, Jaeger or any OTLP backend. Application code depends only on the tiny `opentelemetry-api`; with no exporter configured (the default) every call is a no-op, so monitoring can never take a request down.

- **Traces:** a worker `job.process` span (attempt, outcome) contains an `agent.turn` span with one child per model call (`llm.generate`: tokens, tool-call count) and per tool call (`tool.execute`: tool name, ok, result size). Rejected reports appear as `report.rejected` events (grounding or schema). Session requests get a `session.turn` span.
- **Metrics:** turns by outcome (ok, error, cancelled), tokens by direction, model and tool call durations, report rejections by kind, job outcomes.
- **Logs and traces are linked:** every JSON log line written inside a span carries `trace_id` and `span_id`.
- **Privacy by construction:** spans carry metadata only (tool names, counts, token numbers, statuses), never prompts, log contents, ticket text or tool arguments; every attribute is sanitized to numbers, booleans and short strings, and the session id (a bearer-style capability) is only recorded as a hash prefix.
- **Sampling:** `OTEL_SAMPLE_RATIO` keeps a fraction of traces, parent-based so a trace is kept or dropped as a whole.

```bash
OTEL_EXPORTER=console .venv/bin/uvicorn app.main:app      # spans and metrics printed to stderr
OTEL_EXPORTER=otlp OTEL_ENDPOINT=http://localhost:4318 ... # OTLP/HTTP to a collector (auth headers via the standard OTEL_EXPORTER_OTLP_HEADERS)
.venv/bin/python scripts/show_trace.py --llm gemini --scenario bug-2010   # one investigation as an indented span tree with timings
```

Not instrumented yet: HTTP request spans (add `opentelemetry-instrumentation-fastapi` when a collector exists) and database queries.

## Human approval for write actions

The agent is read-only: it never holds write credentials. When an automatic investigation succeeds, the worker records a **proposal** to post a Jira comment (`GET /actions`, web UI page *Approvals*). Nothing is posted until an `admin` key approves it:

- the comment is rendered from a fixed template, and every model-written field is neutralized first (Jira mentions, links and macros, control and invisible characters are removed), because the text is derived from untrusted logs and tickets;
- approving means sending the **hash of the exact text the reviewer saw**; if the text changed meanwhile the request is refused (409), and the content is verified again right before sending;
- state changes are atomic (`UPDATE ... WHERE status = 'pending'`), so two simultaneous approvals or a double click post once; the proposer cannot approve its own proposal; proposals expire (`APPROVAL_TTL_HOURS`, default 24);
- every step is written to an append-only `audit_log` (database triggers reject UPDATE, DELETE and TRUNCATE; `GET /audit`, admin only).

Posting uses a simulated Jira (a `jira_outbox` table with an idempotency key); a real implementation only needs to satisfy the small `JiraWriter` protocol. Known limitation: a crash exactly while posting leaves the action in `executing` for a human to check. Set `WRITEBACK_PROPOSALS_ENABLED=false` to turn proposals off.

## MCP server

The investigation tools (ticket, logs, deployments, runbook/knowledge search) are also exposed as a [Model Context Protocol](https://modelcontextprotocol.io) server over stdio, so any MCP host can use them: `scripts/mcp_server.py` (`--knowledge` adds semantic search). All tools are declared read-only and share their definitions, argument validation and limits with the agent's own tool registry; the agent's `submit_rca_report` action is deliberately not exposed. Connect it to Claude Code with `claude mcp add infra-debugging -- <project>/.venv/bin/python <project>/scripts/mcp_server.py`.

The agent can also consume its tools **through** MCP (`TOOL_SOURCE=direct|mcp-memory|mcp-stdio|mcp-http`, a server-side setting: a request cannot choose it). The MCP client treats the server as untrusted: tool allowlist, read-only requirement, truncated descriptions, schema/result/time limits. `scripts/compare_tool_transports.py` measures what each layer costs.

### MCP over HTTP

`scripts/mcp_server.py --transport http` serves the same tools over Streamable HTTP at `127.0.0.1:8765/mcp`. Unlike stdio (where being able to launch the process is the authorization), a network port needs real authentication: every request must carry `Authorization: Bearer agk_...`, checked by the same code as the REST API (`app/auth.py`) and requiring the `investigate:run` scope. It is stateless (any instance can serve any request), caps request bodies at 64 KB, validates Host headers against DNS rebinding, refuses to start without a database, and refuses non-local binds unless `--allowed-host` is given. Put TLS in front before exposing it.

To make the agent use a remote server: `TOOL_SOURCE=mcp-http`, `MCP_HTTP_URL=https://...`, `MCP_API_KEY=agk_...` (a dedicated least-privilege key). Settings refuse plain `http` to non-local hosts, since the key would travel in the clear, and the key is masked when settings are printed.

## API authentication

Every endpoint except `/health` and the HMAC-signed `/webhooks/jira` needs `Authorization: Bearer agk_<id>.<secret>`. Keys have roles (`viewer` reads jobs, `operator` also runs investigations and chat, `admin` also approves write actions) and are managed by a CLI, not an HTTP endpoint:

```bash
.venv/bin/python scripts/manage_api_keys.py create --name alice --role operator --expires-in-days 90   # key shown once
.venv/bin/python scripts/manage_api_keys.py list
.venv/bin/python scripts/manage_api_keys.py revoke <key_id>
```

Only a SHA-256 hash of the secret is stored. Failures all return the same 401 (no hint whether a key exists, expired or was revoked), keys in the URL are never accepted, and a database outage returns 503 rather than letting requests through. Sessions belong to the key that created them: other keys get a 404 (admins excepted), so guessing a session id gets you nothing. Paste the key into the top bar of the web UI.

`AUTH_REQUIRED` defaults to `true` (fail closed). For purely local experiments you can set `AUTH_REQUIRED=false` in `.env`; the server logs a warning at startup.

## Session persistence

Chat sessions live in Postgres (`chat_sessions`), so they survive restarts and are shared by several instances (`SESSION_STORE=memory` keeps the old in-process store for local experiments and tests). The full model history is stored as JSON, including Gemini's raw response objects (the thought signatures must be replayed to the model; unknown raw types are refused at save time instead of silently dropped).

Concurrency works across instances with the same ideas as the job queue: a message first takes a **lease** (`busy_until` + a token, one atomic `UPDATE ... RETURNING` that also reads the latest history), a crashed holder's lease simply expires, and every save must show the lease token and the expected `version` (fencing plus optimistic locking), so a slow instance whose lease expired cannot overwrite the work of the one that took over. The lease is released in a shielded scope, because a cancelled request (client disconnect) would otherwise cancel its own cleanup.

Limits: a size cap per session, a per-user session cap (oldest removed), and sessions idle longer than `SESSION_TTL_DAYS` (default 30) are purged at startup, since conversations contain log excerpts. A session that used knowledge search cannot be resumed while the knowledge base is unavailable (it answers 503 rather than continuing with a different tool set). Tool results are stored in plain JSON, so treat the database as holding sensitive operational data. Sessions do not use MCP (a long-lived MCP connection would need its own owner task).

## Chat UI

A Next.js front end lives in `frontend/` (see `frontend/README.md`): the chat with live agent steps and the report card, the automatic-investigation pages (`/jobs`) and the approvals page (`/approvals`). With the backend running:

```bash
cd frontend && npm install && npm run dev   # http://localhost:3000; paste your API key in the top bar
```

The backend allows browser calls from `http://localhost:3000` by default (CORS allowlist, configurable with `CORS_ORIGINS`).

## Project layout

```
app/                 FastAPI backend
  agent/             tool-calling loop, tools, evidence grounding, report schema
  api/               HTTP endpoints (chat, sessions, jobs, webhook, approvals, investigate)
  rag/               chunking, embeddings, ingestion, retrieval, retrieval evaluation
  evals/             scenario generator, scorer, baselines, harness, paired comparison
  llm/               provider-agnostic LLM interface, Gemini client, message codec
  auth.py, actions.py, sessions.py, jobs.py, worker.py, telemetry.py, mcp_*.py, tool_sources.py
frontend/            Next.js UI
scripts/             operations and evaluation CLIs (key management, worker, ingestion, eval, smoke test, ...)
data/                simulated scenarios, knowledge base documents, evaluation queries
docs/deployment.md   container verification, production settings, Cloud Run guide
tests/               pytest suite (real PostgreSQL where it matters, scripted fake LLMs elsewhere)
```

## Testing approach

Fake LLMs make the suite deterministic and free (and the test environment has the model credentials blanked, so no test can call the real model). Database behavior is tested against a real PostgreSQL. Safety-relevant code was **mutation-checked**: break one line at a time and confirm a test fails; this found several weak tests that had passed on the first run. Evaluation and its scoring have their own tests (answerability, leakage, anti-gaming, frozen real-model answers). CI validates every push; a smoke test runs the real containers.

## Known limitations

- Data sources are simulated JSON files; real Jira / log / Git connectors do not exist. The agent, safety and evaluation machinery is built against the tool interface, so only the tool back ends would change.
- Evaluation numbers come from synthetic scenarios and a keyword scorer (adjusted twice after seeing outputs); the held-out set is small. See the caveats under *Results*.
- No rate limiting. The API key lives in `sessionStorage` in the browser (an HttpOnly cookie session would be stronger). Sessions do not use MCP.
- Jira write-back posts to a simulated outbox; a crash exactly while posting leaves the action in `executing` for a human to check.
- Cloud Run deployment is documented in `docs/deployment.md` but was never executed. No load test, no image vulnerability scan, floating base-image tags, no schema migration tool (tables are created idempotently).
- Free-tier Gemini quotas can throttle multi-step investigations and evaluation runs (a control run was lost to a 429).
- Authentication uses one shared database; there is no per-tenant isolation beyond session ownership.
