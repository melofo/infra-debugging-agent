# 简历要点：Infra Debugging Agent（项目部分）

> 这个项目可以直接贴进简历的条目，以及哪些话现在不能写。只含项目本身，不含任何个人工作经历。证据和数字的出处见 `docs/evaluation-report.md` 和 `docs/deployment.md`；岗位对照见 `docs/fde-role-mapping.md`；故事和问答见 `docs/interview-prep.md`。

## 项目条目（单独的 Projects 栏）

**Infra Debugging Agent**: Python, FastAPI, Next.js, PostgreSQL/pgvector, Google Cloud (Cloud Run, Cloud SQL), Gemini, MCP, OpenTelemetry. github.com/melofo/infra-debugging-agent
- Built and deployed an end-to-end incident-investigation agent (Python, FastAPI, Next.js, PostgreSQL/pgvector) on Google Cloud (Cloud Run services and a worker pool, Cloud SQL, Secret Manager, Cloud Build), triggered by Jira webhooks or a chat UI with persistent memory and live step streaming; Gemini is called through a service account with no API keys, and a tool-calling loop over logs, deploys, runbooks and a vector knowledge base returns schema-validated reports with **evidence grounding enforced in code**, human-approved write-back and an append-only audit log.
- Designed a **provider-agnostic LLM layer** (adapters for Gemini and any OpenAI-compatible server) that left the agent loop, safety checks and evaluation unchanged when a locally hosted open model (Gemma 4) was added; diagnosing that model's structured-output failures inside the adapter eliminated every format rejection. Also exposed the tools through an **MCP server**, added routing, chaining and parallel specialist workflows, and built it on a Postgres job queue, API-key roles, **OpenTelemetry** and a real-database CI pipeline.
- Designed an **evaluation pipeline** (generated scenarios, deterministic scorer plus an LLM-judge second opinion, no-model baselines at **0–20%** vs **100%** oracle, held-out set, paired significance tests): a service-listing tool and one system-prompt paragraph took held-out accuracy from **0% to 100%** for about **21% more input tokens**; the same scenarios through the deployed webhook-to-worker path scored **100% Top-1 at ~8 s median**; pgvector RAG beat BM25 on hit@3 (**100% vs 80%**), and hybrid fusion measured worse and was not shipped.

**短版（两条，一页放不下时用）：**第 1 条加第 3 条。

## 自托管开源模型（本地已做，云上是计划）

**状态：** 本地 = 已完成并评估；Cloud Run GPU = **计划，还没做**。简历里只能写已完成的；计划部分做完、拿到真实数字之后才能加进去。

**A. 现在就能用（只含已完成的本地部分）**
> Self-hosted an open-weight model (Gemma 4 26B, quantized) locally with Ollama behind an OpenAI-compatible API, connected it to the agent through a provider adapter, and benchmarked it against Gemini on a held-out set, diagnosing its structured-output failures (format rejections 25 → 0).

**B. 云上部分做完之后追加（模板：尖括号里的数字必须用真实测量值替换；没有数字就不要加）**
> ... then deployed a Gemma 4 model (weights, `config.json` and tokenizer from Hugging Face) on Cloud Run GPUs with vLLM as a private service protected by service-account identity tokens that scales to zero, measuring cold start (<X> s), throughput (<Y> output tokens/s) and cost per investigation ($<Z>) against the Gemini API.

**C. 两部分合并后的完整一句（做完之后用）**
> Self-hosted open-weight Gemma 4 both locally (Ollama, quantized) and on Cloud Run GPUs (vLLM, private endpoint with service-account identity tokens, scaling to zero) behind an OpenAI-compatible API, connected through a provider adapter that left the agent loop unchanged; benchmarked it against Gemini on a held-out set, diagnosed its structured-output failures (format rejections 25 → 0), and measured cold start, throughput and cost per investigation.

**云上部分要测什么（计划，做的时候逐项记下数字）**

| 指标 | 怎么测 | 填进哪里 |
|---|---|---|
| 冷启动时间 | 缩到零之后第一个请求，从发出到收到第一个回答 | B 里的 `<X>` |
| 吞吐（输出 token/秒） | 一次调查的输出 token 数 ÷ 模型调用的时间，取多次中位数 | `<Y>` |
| 单次调查成本 | GPU 计费秒数 × 单价 ÷ 调查次数（单价以官方价格页为准，写明是估算） | `<Z>` |
| 对比 | 同样的场景在 Gemini 上的 token 数和延迟（已有：约 8 秒、约 1.6 万输入 token） | 对比句 |
| 私有性 | 不带令牌的请求返回 401/403，且不会唤醒 GPU | 验证项，不写进简历 |
| 缩到零 | 空闲一段时间后实例数为 0 | 验证项 |
| 准确率 | 只跑一个很小的场景集；小模型预期很差，**不是目标** | 如实记录，不吹 |

**面试里被追问云上部分时（计划做完之前）怎么说：** "本地我做过并评估了；云上的 GPU 部署我设计好了配置和测量方案，因为成本放在了最后，还没执行。" 不要把计划说成已做。

## 不要写（会被追问穿）

- 自托管模型已经部署到 GPU（还没做，做完前只能写本地部分，见上一节 A）；支持 Claude。
- "LLM-judge second opinion" 只实现了，没有在真实模型上验证，被问到答 implemented, not yet validated；一页放不下就删它。
- 36/36 是部署验收：每个场景一次、合成数据、开发集评分器调过，可信的是留出集 6/6。
- ranking 是模型自报的置信度（0 到 1）按代码排序，旧配置在留出集上校准差距为 +0.85，不要说成"我做了排序算法"。
