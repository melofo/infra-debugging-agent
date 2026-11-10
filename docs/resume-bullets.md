# 简历要点：Infra Debugging Agent（项目部分）

> 这个项目可以直接贴进简历的条目，以及哪些话现在不能写。只含项目本身，不含任何个人工作经历。证据和数字的出处见 `docs/evaluation-report.md` 和 `docs/deployment.md`；岗位对照见 `docs/fde-role-mapping.md`；故事和问答见 `docs/interview-prep.md`。

## 项目条目（单独的 Projects 栏）

**Infra Debugging Agent**: Python, FastAPI, Next.js, PostgreSQL/pgvector, Google Cloud (Cloud Run, Cloud SQL), Gemini, MCP, OpenTelemetry. github.com/melofo/infra-debugging-agent
- Built and deployed an end-to-end incident-investigation agent (Python, FastAPI, Next.js, PostgreSQL/pgvector) on Google Cloud (Cloud Run services and a worker pool, Cloud SQL, Secret Manager, Cloud Build), triggered by Jira webhooks or a chat UI with persistent memory and live step streaming; Gemini is called through a service account with no API keys, and a tool-calling loop over logs, deploys, runbooks and a vector knowledge base returns schema-validated reports with **evidence grounding enforced in code**, human-approved write-back and an append-only audit log.
- Designed a **provider-agnostic LLM layer** (adapters for Gemini and any OpenAI-compatible server) that left the agent loop, safety checks and evaluation unchanged when a locally hosted open model (Gemma 4) was added; diagnosing that model's structured-output failures inside the adapter eliminated every format rejection. Also exposed the tools through an **MCP server**, added routing, chaining and parallel specialist workflows, and built it on a Postgres job queue, API-key roles, **OpenTelemetry** and a real-database CI pipeline.
- Designed an **evaluation pipeline** (generated scenarios, deterministic scorer plus an LLM-judge second opinion, no-model baselines at **0–20%** vs **100%** oracle, held-out set, paired significance tests): a service-listing tool and one system-prompt paragraph took held-out accuracy from **0% to 100%** for about **21% more input tokens**; the same scenarios through the deployed webhook-to-worker path scored **100% Top-1 at ~8 s median**; pgvector RAG beat BM25 on hit@3 (**100% vs 80%**), and hybrid fusion measured worse and was not shipped.

**短版（两条，一页放不下时用）：**第 1 条加第 3 条。

## 自托管开源模型（本地和云上都已做）

**状态：** 本地（Ollama，Gemma 4 26B 量化）= 已完成并评估；Cloud Run GPU（vLLM，Gemma 4 **E2B**，最小的模型）= **已部署、测量、然后删除**。

**条目（可以直接贴）**
> Self-hosted open-weight Gemma 4 both locally (Ollama, 26B quantized) and on Cloud Run GPUs (vLLM, E2B in bf16) behind an OpenAI-compatible API, connected through a provider adapter that left the agent loop unchanged; benchmarked the local model against Gemini on a held-out set and diagnosed its structured-output failures (format rejections 25 → 0); on Cloud Run, served it as a private endpoint protected by service-account identity tokens, with weights mounted from a project bucket and scale-to-zero, and measured a cold start of about 5 minutes and about 196 output tokens/s.

**一句短版（一页放不下时）**
> Deployed Gemma 4 on Cloud Run GPUs with vLLM as a private, identity-token-protected service that scales to zero (cold start about 5 min, about 196 output tokens/s), and benchmarked a locally hosted 26B model against Gemini on a held-out set.

**实测的数字（出处：`docs/deployment.md` 3.8 节）**

| 指标 | 结果 |
|---|---|
| 私有性 | 不带令牌 403；带令牌 200；云上 worker 只用自己的服务账号也是 200 |
| 冷启动 | 缩到零之后约 4 分 54 秒（两次一致）；最开始那次（节点上还没有镜像）9 分 36 秒 |
| 缩到零 | 最后一次请求后约 5 分钟自己关闭 |
| 吞吐 | 单路约 196 输出 token/秒（三次：195.6、196.1、196.7） |
| 工具调用 | 带函数定义的请求，正确解析出 `search_logs` 调用 |
| 单价（估算） | GPU 实例按目录价约 $3.2/小时，单路约相当于每百万输出 token $4.5；比托管 API 贵，因为单路请求让 GPU 大部分时间空闲 |
| agent 在这个模型上 | 2 个留出场景 0 个完成（步数用完）；最小的模型，预期如此，不是目标。所以**没有**单次调查成本 |

**面试里怎么说（云上部分）：** "我部署了、测了、删了。最小的模型完成不了这个 agent 的任务，我也没有假装它能；重点是部署本身的工程：L4 没配额只能换 RTX、匿名从 Hugging Face 下载被 429 限流所以把权重放进自己的存储桶、私有端点用身份令牌、缩到零和冷启动、以及一个镜像版本比配置旧导致的 403。"

## 不要写（会被追问穿）

- 云上的小模型能完成 agent 任务（实测 0/2）；单次调查成本（没测）；把 26B 部署到云上（没做）；支持 Claude。
- "LLM-judge second opinion" 只实现了，没有在真实模型上验证，被问到答 implemented, not yet validated；一页放不下就删它。
- 36/36 是部署验收：每个场景一次、合成数据、开发集评分器调过，可信的是留出集 6/6。
- ranking 是模型自报的置信度（0 到 1）按代码排序，旧配置在留出集上校准差距为 +0.85，不要说成"我做了排序算法"。
