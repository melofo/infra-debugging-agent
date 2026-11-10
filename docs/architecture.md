# Architecture

How the pieces fit, where the trust boundaries are, and why each major decision was made. Data sources are simulated (JSON scenarios); everything around the agent is real. Numbers quoted here come from the test suite, the evaluation runs and the container checks described in the other documents.

## 1. Components

```mermaid
flowchart LR
  Jira["Jira Cloud<br/>(simulated by scripts/simulate_jira.py)"] -- "signed webhook<br/>HMAC-SHA256" --> API
  UI["Next.js UI<br/>chat, jobs, approvals"] -- "Bearer API key" --> API

  subgraph Backend ["Backend (one image, two roles)"]
    API["FastAPI<br/>auth, sessions, jobs, approvals, SSE"]
    W["Worker<br/>(in-process or separate container)"]
    AG["Agent loop<br/>tool calling, evidence grounding"]
    REG["Tool registry<br/>allowlist + validated arguments"]
    API --> AG
    W --> AG
    AG --> REG
  end

  API <--> PG[("PostgreSQL + pgvector<br/>jobs, sessions, api_keys,<br/>proposed_actions, audit_log,<br/>knowledge chunks")]
  W <--> PG
  AG <--> LLM["Gemini<br/>(behind an LLMClient interface)"]
  REG --> DATA[("Simulated data<br/>tickets, logs, deploys")]
  REG --> KB["Knowledge search<br/>runbooks + past incidents"]
  KB --> PG
  REG -. "optional: tools over MCP<br/>stdio or authenticated HTTP" .-> MCP["MCP server"]
  MCP --> REG
```

- **One agent, two entry points.** A Jira "Bug created" event and a chat message end up in the same agent loop (`app/agent/loop.py`). Jira goes through a durable queue and a worker; chat goes through sessions with streaming.
- **The model proposes, the backend executes.** The model only emits tool-call requests. Every call goes through an allowlist registry that validates arguments and caps result sizes. Nothing the model writes reaches an external system without passing a human.
- **Read-only by construction.** The agent holds no write credentials. The only write path is "propose a Jira comment", executed after human approval.

## 2. One automatic investigation, end to end

```mermaid
sequenceDiagram
  autonumber
  participant J as Jira
  participant A as API
  participant DB as PostgreSQL
  participant W as Worker
  participant G as Agent + Gemini
  participant H as Human (admin)

  J->>A: POST /webhooks/jira (signed)
  A->>A: verify HMAC over the raw body, check size and event type
  A->>DB: INSERT job (event_id UNIQUE = idempotency)
  A-->>J: 202 Accepted (200 if duplicate)
  W->>DB: claim job (FOR UPDATE SKIP LOCKED, fenced by attempt)
  W->>G: investigate(ticket)
  loop until the report is accepted (bounded steps)
    G->>G: get_ticket, search_logs, get_recent_deploys, list_services, search_knowledge
    G->>G: submit report -> evidence grounding check
    Note over G: a reference must appear verbatim in a successful tool result of the right kind, otherwise the report goes back to the model
  end
  W->>DB: complete job (result) + INSERT proposed comment (pending)
  H->>A: GET /actions, reviews the exact text
  H->>A: POST /actions/{id}/approve with the content hash he saw
  A->>DB: atomic pending -> approved -> executing -> executed, plus audit rows
  A->>DB: write to the (simulated) Jira outbox, once, idempotency key
```

Failure handling along the way: transient model errors are retried with exponential backoff, bad data fails permanently without retrying, a job whose worker disappeared is reclaimed after its lease, and a worker that is shut down puts its job back without using an attempt.

## 3. Trust boundaries

Everything the model reads is treated as untrusted data, and so is the model's output.

| Untrusted input | Where it can hurt | Control |
|---|---|---|
| Webhook body | forged events, huge payloads | HMAC over the raw body with constant-time comparison, fail closed without a secret, size limit, uniform status codes chosen for Jira's retry rules |
| Ticket text, logs, runbooks | prompt injection that steers the agent | the agent is read-only; the model's claims must be backed by verbatim evidence; the posted comment is rendered from a fixed template with markup neutralized; a human approves the exact text |
| The model's report | fabricated evidence | evidence grounding in code (not in the prompt), source type bound to the tool that may supply it, too-short references rejected, bounded retries |
| An MCP server | malicious tool descriptions, extra tools, oversized or endless results | client-side allowlist, read-only requirement, truncated descriptions, schema/result/time/page limits, no sampling callbacks, tool source is server configuration and never request-selectable |
| API callers | impersonation, session guessing | hashed keys with roles, uniform 401, 404 (not 403) for someone else's session, fail closed when the database is down |
| Traces and logs | leaking content into observability systems | spans carry metadata only, all attributes sanitized, the session id is hashed |

## 4. Data model (PostgreSQL)

| Table | Purpose | Notable design |
|---|---|---|
| `investigation_jobs` | durable queue and results | `event_id` UNIQUE, status check constraint, partial indexes, lease and fencing columns |
| `chat_sessions` | conversation state | full model history as JSON (including provider raw objects), `version` for optimistic locking, `busy_until` + token as a lease, `agent_variant` fixed at creation |
| `api_keys` | authentication | only a SHA-256 hash of the secret, roles, expiry, revocation, unique active name |
| `proposed_actions` | write actions awaiting approval | content hash, expiry, atomic state machine, unique per job |
| `audit_log` | who did what | append-only, enforced by triggers that reject update, delete and truncate |
| `jira_outbox` | simulated Jira | idempotency key, so a retry cannot post twice |
| `knowledge_documents`, `knowledge_chunks` | RAG | `vector(768)` with an HNSW cosine index; ingestion swaps everything in one transaction |

Schema creation is idempotent and guarded by a Postgres advisory lock, because several instances starting together would otherwise race.

## 5. Key decisions and trade-offs

| Decision | Alternatives | Why this one | What it costs |
|---|---|---|---|
| Hand-written agent loop (plus an SDK variant for comparison) | LangGraph, framework function calling | every step is visible and controllable: grounding checks, rollback, step limits, tracing and approval hooks go exactly where needed | more code to own |
| Evidence verified in code | stricter prompts | prompts proved unreliable (the model kept citing tool names); code is testable and cannot be talked out of it | a verbatim rule can reject a correct paraphrase (it never did with real Gemini: 0 rejections among the 126 reports produced in 129 evaluation runs) |
| Postgres as the job queue | Redis, RabbitMQ, Kafka | jobs must be durable and queryable anyway; one fewer component; volume is minutes apart | would be replaced if throughput grew by orders of magnitude |
| pgvector in the same Postgres | a dedicated vector database | corpus is small; transactional ingestion; filters and joins in SQL | HNSW post-filtering needs care (the inner query must stay a pure ANN query) |
| Pure vector search, no hybrid | BM25 + vector with rank fusion | measured: hybrid was worse than pure vector on this corpus (hit@3 0.94 vs 1.00) | conclusion is limited by a small, author-labeled query set |
| Sessions in Postgres with lease + version | sticky routing, in-process locks | several instances can serve one session; a crashed holder's lease just expires; a slow instance cannot overwrite a newer one | extra queries per turn |
| API keys instead of OAuth | an identity provider | enough for a service-to-service and operator tool; shows the mechanics (hashing, roles, constant time) | no SSO, key kept in `sessionStorage` in the browser |
| Human approval for writes | autonomous write-back | the text derives from untrusted data; approval is bound to a content hash so what is approved is what is sent | a human is in the loop |
| MCP: stdio and authenticated HTTP | only direct calls | standard way to expose tools to other hosts; the client side stays defensive | per-call latency (about 12x in memory, 66x over stdio, measured; small next to a model call) |
| Evaluation with baselines, a held-out set and a paired sign test | accuracy percentage only | a number means nothing without a floor and a ceiling; 30 noisy runs need paired comparison | synthetic scenarios, keyword scorer |
| OpenTelemetry with sanitized attributes | plain logs | one model for traces, metrics and logs; vendor neutral | an extra dependency (a no-op when unconfigured) |

## 5a. Workflow patterns around the agent (`app/agent/workflows.py`)

The production path is still one agent. On top of it sit three small, optional patterns where the **code** fixes the steps and the model works inside each step (as opposed to the agent loop, where the model decides the next step). A "specialist" is the same `InvestigationAgent` with the same tools and rules plus one line saying what to check first, so evidence grounding, read-only tools and step limits are unchanged.

| Pattern | What it does | Properties that are tested |
|---|---|---|
| Routing | One cheap model call classifies the ticket (`deploy`, `config`, `infrastructure`, `general`); only the chosen specialist runs | any answer outside the list falls back to `general`; a failing classifier does not fail the investigation |
| Parallelization | All specialists run at the same time; hypotheses are merged, sorted by confidence, top 5 kept | really concurrent; one failed specialist does not sink the others; an unexpected exception is not swallowed |
| Chaining | route, then one specialist, then a gate: if the top confidence is below 0.6, escalate to the parallel search | cheap path stops when confident; token and call accounting covers every step |

Limits, stated plainly: the gate uses the model's self-reported confidence, which was badly calibrated in v1 (+0.85) and acceptable in v2 (-0.12), so it is an escalation signal and not proof of correctness. The patterns were verified with a scripted fake model only; they have **not** been evaluated on the real model, so there is no claim that they beat the single agent. Parallel specialists cost roughly three times one investigation. The focus lines are deliberately generic so they do not name the held-out incident types (a test checks this). A framework such as ADK or LangGraph would replace this orchestration code, not the agent loop or the grounding.

## 6. Failure modes and how they are handled

| Failure | Behavior |
|---|---|
| Model returns 429/503 | retried with exponential backoff; in the queue, the job is retried later; in evaluation, counted as an error (never dropped) |
| Model never submits a report | bounded by a step limit, then the turn fails and is rolled back (history restored) |
| Model fabricates a citation | report rejected with the failing evidence; after two rejections the turn fails rather than delivering an untraceable conclusion |
| Database down | uniform 503 for protected endpoints (fail closed); chat and queue features degrade, `/health` stays a pure liveness check |
| Worker crashes mid-job | lease expires, job reclaimed; fencing stops the old worker from overwriting the new one's result |
| Client disconnects mid-stream | agent task cancelled, history rolled back, session lease released (in a shielded scope, because a cancelled task would otherwise cancel its own cleanup) |
| Two admins approve at once | one atomic `UPDATE ... WHERE status = 'pending'` wins; the other gets 409; the comment is posted once |
| Several instances start together | advisory lock on schema creation |
| Stuck worker (event loop blocked) | heartbeat goes stale, container healthcheck fails, the orchestrator can restart it |
| SIGTERM | uvicorn is PID 1 and shuts down gracefully (worker stops first, then the pool); verified: exit 0 in under a second |

## 7. Operations

- **Containers:** multi-stage images, non-root user, no secrets or test tools inside, `ENVIRONMENT=production` refuses unsafe settings at startup.
- **CI:** backend tests against a real pgvector database (a missing database fails the run instead of skipping tests), frontend lint, type check, tests and build, a smoke test of the real containers, a non-blocking dependency audit.
- **Deployed shape (Google Cloud):** API and frontend as Cloud Run services, the worker as a Cloud Run worker pool (same image, different start command), Cloud SQL with pgvector for queue, sessions and knowledge, one least-privilege service account used for the database connector and for Gemini, secrets in Secret Manager. Details and what was verified: `docs/deployment.md`.
- **Observability:** traces (`job.process` > `agent.turn` > `llm.generate` and `tool.execute`), metrics, JSON logs that carry `trace_id`. On Google Cloud the traces go straight to Cloud Trace (`OTEL_EXPORTER=gcp-trace`, runtime service account, no collector); metrics are not exported there.

## 8. What would change for a real deployment

1. Replace the three simulated tool back ends with real Jira, log and Git/CI connectors, each with its own read-only credential held by the tool (never by the model).
2. ~~Gemini through the service account~~ (done: `LLM_PROVIDER=vertex`, used on the deployed system) and a second provider adapter for any OpenAI-compatible server (done, `app/llm/openai_compat.py`).
3. A cookie-based session in place of the API key in the browser, SSO for operators.
4. A real migration tool (Alembic) once columns change; a database role for the audit log that can only insert and select.
5. Rate limiting, a load test, image scanning, pinned base-image digests and pinned action SHAs.
6. A larger and partly human-written evaluation set from real incidents; an LLM judge as a second opinion on disputed cases.
