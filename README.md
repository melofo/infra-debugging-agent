# Infra Debugging Agent

An AI agent that investigates production incidents. Given a Jira ticket, it queries logs, recent deployments and runbooks, compares root-cause hypotheses, and returns an **evidence-backed RCA report**.

> Status: work in progress (backend + agent core done; chat UI, Jira webhook, real integrations and deployment are planned).
> Version 1 is **read-only**: the agent can only query data, never change anything.

## What works today

- `POST /investigate` runs the agent on a simulated incident (`data/scenarios/bug-1042.json`) and returns the report, every tool call it made, and token/latency stats.
- Two interchangeable agent implementations, selectable with `mode`:
  - `loop` – a hand-written tool-calling loop (`app/agent/loop.py`): every step is visible and controllable.
  - `sdk` – the Google GenAI SDK's automatic function calling (`app/agent/sdk_afc.py`): much shorter, but the loop is a black box.
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

## Known limitations

- No authentication or rate limiting yet: run it locally only.
- Data sources are simulated JSON files; real Jira / log / Git connectors are planned.
- Free-tier Gemini quotas (about 5 requests/minute) can throttle multi-step investigations.
