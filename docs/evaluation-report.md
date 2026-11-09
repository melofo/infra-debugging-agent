# Evaluation report

What was measured, how, and how far the numbers can be trusted. Everything below can be reproduced from the repository (commands at the end). Real-model runs used Gemini on the free tier; the result files are kept locally and are not committed.

## 1. Questions

1. Does the agent find the root cause, and is it better than trivial strategies?
2. Where does it fail, and is that the model, the data or the scoring?
3. Does a change to the agent (a new tool and prompt) help, including on incident types it was not tuned on?
4. Is a report's evidence traceable, and does the traceability check get in the model's way?
5. What does an investigation cost in model calls, tokens and time?

## 2. Dataset

36 incident scenarios generated deterministically from archetypes by `app/evals/scenarios.py` (the committed files are tested to equal the generator's output). Each scenario is a ticket, logs, deployments and runbooks; the answer key is stored apart and is unreachable from the tools.

| Split | Size | Content |
|---|---|---|
| Development | 30 | 10 root-cause types x easy / medium / hard: connection pool, code regression, memory leak, slow dependency, expired certificate, disk full from debug logging, rotated secret, traffic spike, missing index, feature flag |
| Held-out | 6 | 3 types the development set does not contain, 2 each: autoscaler limit lowered, DNS record changed, network policy blocking egress |

Built so that a good agent and a bad one score differently:

- **Half of the development scenarios have no culprit deploy** (certificate, secret, traffic, dependency, flag), so "blame the latest deploy" cannot win.
- **Hard scenarios contain an innocent deploy shortly before the incident** as a distractor.
- **Noise in every scenario**: normal slow requests, cache misses, harmless warnings.
- **Evidence in another service's logs** for secret rotation and feature flags (`secrets-manager`, `config-service`) and for the held-out types.
- **Symptom-only tickets** and **runbooks that list several candidate causes**, so reading the data is required.

## 3. Scoring

Deterministic, free and unit-tested (`app/evals/scoring.py`). A hypothesis is correct when it contains **all incident-specific facts** (`must_have`: a config key, flag name, dependency name or commit hash, always something found in the incident's own logs or deploys) **and** matches **at least two concept groups** (synonyms joined with `|`). Top-1 looks at the first hypothesis, Top-3 at the first three. Errors, timeouts and quota failures count as wrong answers and stay in the denominator.

Why not an LLM judge: it is slower, costs money, is not reproducible, and tends to favor well-written answers. Keywords are crude but explainable, and the scorer itself is tested:

- the ticket text alone never passes;
- no single runbook, and no combination of runbooks, passes;
- every category's answer fails on every other category;
- the true root-cause text always passes;
- real model answers judged correct or incorrect during auditing are frozen as test cases;
- an incident-specific fact may not appear in the ticket or in any runbook.

## 4. Reference lines

They run through the same agent loop, tools and grounding check, without a model.

| Reference | Top-1 on the 30 development scenarios | What it tells you |
|---|---|---|
| Oracle (reads the answer key) | 100% | every case is answerable and its evidence is groundable, so scorer and data agree |
| Blame the latest deploy | 20% (0% on hard and on no-culprit cases) | the set cannot be solved by the obvious heuristic |
| Copy the best runbook | 0% | documentation alone does not win |
| Echo the ticket | 0% | the ticket does not leak the answer |

Building these found a defect in my own scorer: copying a runbook first scored **30%**, because runbooks list the true cause among several. Requiring incident-specific facts brought it to 0%.

## 5. Results with the real model (Gemini)

### 5.1 First runs and the audit of the misses

The original agent ("v1") was run twice on the development set. The **first run scored 22/30 under the original scorer**, with 8 misses. Every miss was read against the model's actual answer:

| Misses (first run) | Verdict | Action |
|---|---|---|
| traffic spike x3, slow dependency x1 | the model was right; my scorer demanded a keyword the answer did not contain | scorer fixed under a stated rule, real answers frozen as tests |
| feature flag x3 | genuine: the model stopped at the proximate failure ("the engine does not support JPY") and never found that a flag had been flipped, logged by `config-service` | motivated the experiment below |
| secret rotation x1 | ran out of its 8-step budget | step budget raised to 12 in the experiment |

The **second run of the same agent scored 24/30** (80%, 95% interval 63-90%) with the corrected scorer: **15/15 when a deploy caused the incident, 9/15 otherwise.** The two runs differ by about one case once the scorer is held constant, which is the noise floor for this set. The second run is the baseline for the experiment.

### 5.2 The experiment (controlled, with a held-out set)

Hypothesis from the data: the model never looked in the other service's logs, probably because it did not know that service existed. Change ("v2"): a read-only `list_services` tool and a generic paragraph telling the model that not every incident is a deploy and that other changes are often logged elsewhere. The paragraph names no service and no held-out incident type (a test forbids it).

| | v1 | v2 |
|---|---|---|
| Development, 30 scenarios | 24/30 | **30/30** (6 newly correct, 0 newly wrong, paired sign test p = 0.031) |
| Held-out, 6 scenarios x 3 runs | **0/18** | **18/18** (per-scenario sign test p = 0.031) |
| Looked in the service holding the key evidence (when it was not the ticket's own service) | 2/6 dev, 0/18 held-out | 6/6 dev, 18/18 held-out |

The last row is the mechanism check: the improvement came from doing what the hypothesis said.

### 5.3 Calibration, traceability

| | Mean confidence minus accuracy | Cited the key evidence |
|---|---|---|
| v1 on held-out | **+0.85** (about 85% sure, 0% right) | 12/18 |
| v2 on held-out | -0.12 (slightly modest) | 18/18 |
| v1 on development | +0.04 | 25/28 |
| v2 on development | -0.10 | 30/30 |

An agent that is confidently wrong is the dangerous case, and v1 showed it exactly where it lacked a capability.

**Evidence grounding never got in the way:** across the 129 valid real-model runs (126 produced a report) there were **0** rejections for unverifiable evidence and 0 for invalid format.

### 5.4 Cost and latency

Per investigation, averages over runs that produced a report.

| Run | Model calls | Tool calls | Input tokens | Output tokens | Latency p50 / p95 |
|---|---|---|---|---|---|
| v1, development | 6.75 | 5.29 | 15,467 | 463 | 7.4 s / 31.9 s |
| v2, development | 7.30 | 5.57 | 18,678 | 487 | 7.1 s / 31.4 s |
| v1, held-out | 6.78 | 5.28 | 14,928 | 451 | 6.1 s / 32.2 s |
| v2, held-out | 8.22 | 6.72 | 21,267 | 506 | 8.0 s / 32.4 s |

v2 costs about **+21% input tokens and +8% model calls** on the development set for roughly the same latency. Input tokens dominate (about 30-40x the output), because every model call re-sends the whole history, so cost per investigation is approximately `input_tokens x input_price + output_tokens x output_price`. At v2's averages, **1,000 investigations are about 18.7 million input and 0.49 million output tokens**; multiply by your model's current prices (this project does not hard-code prices; pass `--price-in` and `--price-out` to the evaluation script to get an estimate). In an interview the sharper point is that the cost is controlled by design: a step limit per turn, a turn limit per session, a size cap on tool results, and bounded grounding retries.

### 5.5 Sampling temperature

Question: does lowering the sampling temperature to 0 (the model picks the most likely token at every step, so runs should vary less) improve v2? Google's guidance for Gemini 3 models is to leave temperature at the default, because lower values may degrade reasoning or cause loops, so this was checked rather than assumed. Same 6 held-out scenarios, 3 runs each, v2, 12 steps, only the temperature changed (`--temperature 0`, recorded in the results file).

| | Default temperature | Temperature 0 |
|---|---|---|
| Top-1 | 18/18 | 18/18 |
| Errors, grounding rejections | 0, 0 | 0, 0 |
| Calibration gap | -0.12 | -0.11 |
| Model calls, input tokens | 8.22, 21,267 | 8.33, 21,363 |
| Latency p50 / p95 | 8.0 s / 32.4 s | 16.5 s / 75.7 s |

Result: **no measurable effect on accuracy** (paired sign test on 6 scenarios: no case changed, p = 1.0). The test cannot show an improvement here, because the default is already at the ceiling (18/18), so the only thing it can establish is "no worse". Latency was higher at temperature 0 in this one sitting; with 18 runs and a rate-limited free tier (retries wait 8 s or more) this is not evidence that temperature 0 is slower, and it was not investigated further. Decision: **keep the default** (`LLM_TEMPERATURE` unset); the setting stays available and is recorded per run. A harder set, where the default is not already perfect, would be needed to see whether lower temperature reduces run-to-run variation.

### 5.6 A self-hosted open model (Gemma 4 26B through Ollama)

Question: can the same agent run on a model hosted on my own machine, and what does it cost in quality? The agent code is unchanged; a second adapter (`app/llm/openai_compat.py`, `LLM_PROVIDER=openai-compatible`) translates our message and tool formats to the OpenAI chat format that Ollama, vLLM and OpenAI speak. Same held-out set (6 scenarios x 3 runs), v2, 12 steps, temperature 0, local Ollama with a 32K context, on a 64 GB Mac. The Gemini run used the default temperature.

| | Gemini | Gemma 4, first run | Gemma 4, after the schema fix |
|---|---|---|---|
| Top-1 | 18/18 | 9/18 | 13/18 |
| Runs failed because the 12 steps ran out | 0 | 9 | 5 |
| Reports rejected for invalid format | 0 | 25 | 0 |
| Runs with a report rejected for unverifiable evidence | 0 | 0 | 12 of the 13 that finished |
| Accuracy among runs that finished | 18/18 | 9/9 | 13/13 |
| Model calls, input / output tokens per run | 8.2, 21k / 0.5k | 11.9, 28k / 5.5k | 10.8, 25k / 3.9k |
| Latency p50 / p95 | 8 s / 32 s | 95 s / 447 s | 88 s / 113 s |
| Input tokens served from the server's cache | n/a | 89% | 91% |

What happened, in order:

1. **The smaller model understood the incidents but could not fill the report.** All 9 failures were "ran out of steps", none a wrong diagnosis. A trace showed it writing `hypothesis` instead of `root_cause` and evidence as plain strings, correcting itself step by step until the budget ended.
2. **Cause: JSON Schema references.** Our report schema nests objects through `$ref` pointers. Gemini's SDK expands them for us; an OpenAI-compatible server may not, so the model never saw the evidence fields. Expanding the references inside the adapter (the core code is untouched) removed all 25 format rejections.
3. **New bottleneck: verbatim evidence.** 12 of the 13 finished runs had a report rejected once because a reference was a paraphrase, not the exact tool output (Gemini: none). The grounding check did its job; the smaller model needs more steps to satisfy it, which is where the remaining 5 failures come from.

Reading the numbers: accuracy 50% -> 72% is not distinguishable from chance on 6 scenarios (sign test p = 0.625), and 72% against Gemini's 100% is not either (p = 0.125); four scenarios were unstable across repeats. The solid results are the mechanism ones: format rejections 25 -> 0, and the finished runs were all correct. The remaining gap is about following the output contract and the step budget, not about diagnosis. Limits: one model and one machine (latency and throughput are specific to it), synthetic scenarios, not yet tried with more steps or a stricter rejection message, and not yet run on a cloud GPU.

### 5.7 The deployed system, end to end

Question: does the deployed system (not just the agent code) work? `scripts/eval_deployed.py` sends each scenario as a signed Jira webhook to the public API on Google Cloud, waits for the job through `GET /jobs/{id}` and scores the report with the same scorer. Everything in the path is real: authentication, the Postgres queue, the worker pool, Cloud SQL with pgvector, and Gemini reached through the service account (a small, fast model; one run per scenario).

| | Result |
|---|---|
| Top-1 / Top-3, all 36 scenarios | 36 / 36, 36 / 36 |
| Development set / held-out set | 30 / 30, 6 / 6 |
| Scenarios with / without a culprit deploy | 15 / 15, 21 / 21 |
| Errors (not queued, job failed, unreadable) | 0 |
| Model calls, tokens per investigation | 7.0 calls, about 16,200 in / 480 out |
| Wall time from webhook to result | p50 8.1 s, p95 10.9 s (includes queueing; the worker handles one job at a time) |

Reading it: this is an acceptance check of the deployment, not new evidence about the agent. The development scorer was adjusted after seeing outputs, so the held-out 6 of 6 is the figure that carries weight, and each scenario ran once. It shows that the authentication, queue, worker, database and model channel all work together and what a real investigation costs in calls and tokens.

### 5.8 Retrieval (separate evaluation of the knowledge search)

35 labeled queries, document-level hit@3 and mean reciprocal rank (MRR):

| Strategy | hit@3 | MRR |
|---|---|---|
| Random | 0.34 | 0.30 |
| BM25 keyword search | 0.80 | 0.76 |
| Vector, whole documents | 1.00 | 1.00 |
| Vector, chunks of 1000 (used) | 1.00 | 0.99 |
| Vector, chunks of 400 | 1.00 | 0.99 |
| Hybrid BM25 + vector (rank fusion) | 0.94 | 0.88 |

On queries built to share almost no words with their answers, BM25 hit@3 is 0.25 against 1.00 for vectors. The hybrid was **worse** than pure vector here, so it was not added.

## 6. What went wrong, and what it taught

- **The scorer was too strict for some right answers** and too lenient for a runbook copy. Both were found by auditing and by baselines, not by looking at the headline number.
- **The scorer was adjusted twice after seeing outputs** (four false negatives, then a certificate wording). Both runs being compared were re-scored with the same rule, and the held-out scorer was not touched, but the development numbers should be read as optimistic.
- **The control run was lost.** "v1 with 12 steps" on the development set failed with 29 of 30 runs hitting the Gemini free-tier quota (`429`). The file is unusable, and the comparison script now warns when a run is mostly errors instead of printing a normal-looking number. Consequence: on the development set the step budget and the new tool are not separated. On the held-out set v1 averaged 6.8 model calls and never reached its limit of 8, so budget cannot explain its 0/18.
- **I once put the held-out incident types into the prompt** ("scaling, DNS, network policy") while writing the paragraph. A test that forbids those words in the prompt caught it before any run.

## 7. Threats to validity

- The scenarios are synthetic and come from one generator, so their structure is similar and the real world is messier. Treat results as comparisons between versions of this agent, not as accuracy to expect in production.
- 30 development cases give wide intervals (one case is 3.3 points); 6 held-out scenarios is small even with 3 runs each. A sign test on 6 scenarios can at best reach p = 0.031.
- Keyword scoring is coarse. An unusual but correct phrasing can score wrong, a coincidental phrasing can score right (mitigated by the tests above, not eliminated).
- One model family and one prompt style were tested. Gemini's free tier also limits how many repeats are affordable.
- The held-out types are all "non-deploy change logged by another service", which is the domain the improvement targets; the development set is what checks for regressions elsewhere.

## 8. Reproduce

```bash
python scripts/build_eval_scenarios.py --check                 # committed scenarios equal the generator
python scripts/eval_agent.py --llm oracle                      # reference lines, no model, free
python scripts/eval_agent.py --llm latest_deploy
python scripts/eval_agent.py --llm gemini --agent v2 --max-steps 12 --sleep 15 --out eval_results/v2-dev.jsonl
python scripts/eval_agent.py --llm gemini --split holdout --agent v2 --max-steps 12 --repeat 3 --out eval_results/v2-holdout.jsonl
python scripts/eval_agent.py --llm gemini --split holdout --agent v2 --max-steps 12 --repeat 3 --temperature 0 --out eval_results/v2-holdout-temp0.jsonl
LLM_PROVIDER=openai-compatible LLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=gemma4:26b python scripts/eval_agent.py --llm real --split holdout --agent v2 --max-steps 12 --repeat 3 --temperature 0 --out eval_results/gemma4-26b-holdout.jsonl
JIRA_WEBHOOK_SECRET=... AGENT_API_KEY=... python scripts/eval_deployed.py --url https://your-api --split all   # a deployed instance, end to end
python scripts/compare_evals.py eval_results/run2.jsonl eval_results/v2-dev.jsonl    # paired comparison, sign test
python scripts/eval_agent.py --rescore eval_results/run2.jsonl                       # re-score saved results with the current rule
python scripts/eval_retrieval.py                                                     # retrieval evaluation
```
