# Five-minute demo

A script for showing the project live. Each beat says what to run, what to point at and the one sentence to say. Rehearse it once with the checklist below; every command was run against this repository.

## Before you start (one time, about 2 minutes)

```bash
cd ~/infra-debugging-agent
docker compose up -d db                              # database (leave it running)
.venv/bin/uvicorn app.main:app --reload              # terminal 1: backend (the worker runs inside it)
cd frontend && npm run dev                           # terminal 2: UI on http://localhost:3000
.venv/bin/python scripts/manage_api_keys.py create --name demo --role admin   # shown once: paste it in the UI's top bar
export KEY=agk_...                                   # the same key, for the curl command in the approval beat
```

Have these ready: the UI open at `/` with the key pasted, a terminal in the project directory, and `docs/evaluation-report.md` open at the results tables.

**If the model quota is exhausted** (Gemini free tier): do beats 1 and 3 with the no-model stand-in `--llm oracle` (shown below), skip the live chat, and say so. Never hide it; the evaluation beat does not need a model.

## The beats

### 0:00 The problem (30 s)
> "On-call engineers lose time hunting through logs, deploys and runbooks. I built an agent that does the first investigation and gives an evidence-backed root-cause report. It is read-only, and it cannot invent its sources."

Show the architecture diagram in `docs/architecture.md` (the first Mermaid block). Point at: webhook and chat into one agent; the database in the middle; the human at the end.

### 0:30 A ticket arrives and gets investigated (75 s)
```bash
.venv/bin/python scripts/simulate_jira.py created --wait      # plays Jira: signed webhook, waits for the report
```
Open `http://localhost:3000/jobs`, then the job.
> "A signed Jira event came in, was verified, queued in Postgres and picked up by a worker. These are the steps the agent took and the report it produced."

Point at: the step list (ticket, logs, deploys, knowledge search), the ranked hypotheses, **each evidence item's exact reference** (a commit hash, a verbatim log line, a runbook id).

### 1:45 "It cannot make up evidence" (60 s)
```bash
.venv/bin/pytest tests/test_grounding.py -q -k "fabricated_report_is_sent_back" -v
```
Then open `app/agent/grounding.py`, scroll to the rules in the docstring.
> "Prompts proved unreliable: the model kept citing tool names as evidence. So the rule is in code: a reference must appear verbatim in a successful tool result of the right kind, a log reference cannot come from the ticket, and a rejected report goes back to the model. With real Gemini it was rejected zero times in 126 reports, so it costs nothing when the model behaves, and it catches it when not."

### 2:45 Human approval for the one write (45 s)
Open `http://localhost:3000/approvals`: the proposed Jira comment for the job just run.
> "The agent never holds write access. It proposes this exact text. Approving sends back the hash of what I reviewed, so what is approved is what is posted; the state change is atomic, so two people clicking posts once."

Approve it with the admin key, then:
```bash
curl -s -H "Authorization: Bearer $KEY" "http://localhost:8000/audit?limit=5" | python3 -m json.tool | head -30
```
> "Every step is in an append-only audit log; database triggers refuse updates and deletes."

### 3:30 How do I know it is any good? (60 s)
```bash
.venv/bin/python scripts/eval_agent.py --llm oracle        | grep -E "^=== |Top-1"
.venv/bin/python scripts/eval_agent.py --llm latest_deploy | grep -E "^=== |Top-1"
```
Then show the results tables in `docs/evaluation-report.md` (sections 4 and 5.2).
> "36 generated incidents, half without any culprit deploy. A no-model baseline that blames the latest deploy gets 20%, copying a runbook gets 0%. Gemini got 24 of 30; every miss was audited. The failures were the incidents where a flag or a secret changed in another service, and the model never looked there. I added a service-listing tool and one generic sentence of guidance, and tested it on three incident types the prompt never mentions: 0 of 18 became 18 of 18, and the check that it now looks in the other service's logs went from 0 of 18 to 18 of 18."

### 4:30 Observability and close (30 s)
```bash
.venv/bin/python scripts/show_trace.py --scenario bug-2010
```
> "Traces and metrics are OpenTelemetry, with metadata only: no prompts or log contents leave the process. CI is green, the containers are verified end to end. What it is not: the data sources are simulated, and the evaluation is synthetic. The next step is real connectors and a real-incident evaluation set."

## If something goes wrong

| Symptom | Do this |
|---|---|
| UI shows "Missing or invalid API key" | paste the key again (it lives in `sessionStorage`, gone when the tab closes) |
| `simulate_jira.py` reports the job stuck in `queued` | the worker only starts when the model is configured; run `.venv/bin/python scripts/check_env.py` |
| Gemini returns 429 | quota; switch to the oracle stand-in and say so |
| Job fails with a model error | open the job page: the error is shown without internals; the logs have details |

## Questions you should expect during the demo

- *"What stops it from approving its own comment?"* The proposer cannot be the approver, and approval needs an admin role.
- *"What if the log contains instructions?"* Everything the model reads is untrusted; it is read-only, evidence must be verbatim, the comment is rendered from a fixed template with markup neutralized and a human reads it.
- *"Why not a framework?"* There is an SDK variant for comparison; the hand-written loop is where grounding, rollback, limits, tracing and approval hooks sit.
- More in `docs/architecture.md` (decisions table) and the evaluation limits in `docs/evaluation-report.md` section 7.
