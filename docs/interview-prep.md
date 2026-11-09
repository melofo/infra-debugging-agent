# 面试准备：Infra Debugging Agent

用法：先背 §1 的 pitch 和 §3 的数字，再练 §4 的故事，最后过一遍 §5、§6 的问答。每个回答都是"英文说法 + 中文要点"。
原则：**说真实发生的事，主动讲局限**。这个项目最强的地方不是功能多，而是你能讲清楚"怎么知道它好不好、哪里不可信"。

---

## 1. Pitch

### 30 秒
> I built an agent that does the first pass of a production incident investigation: given a Jira ticket, it reads logs, deploys and runbooks and returns a root-cause report. The part I care most about is trust: every piece of evidence is verified in code against what the tools actually returned, the agent is read-only, and its one write, a Jira comment, needs a human to approve the exact text. And I measured it properly: baselines, a held-out set, and a controlled experiment that took held-out accuracy from 0 of 18 to 18 of 18.

要点：问题 → 做了什么 → 信任（代码校验证据、只读、人工审批）→ 度量（基线、留出集、对照实验）。

### 2 分钟（在 30 秒基础上加）
> Two entry points share one agent: a Jira webhook goes through a durable Postgres queue and a worker, and a chat UI streams the steps live. The model only proposes tool calls; a registry with an allowlist and validated arguments executes them. Reports are rejected if a cited reference is not found verbatim in a successful tool result of the right kind, because prompts turned out to be unreliable: the model kept citing tool names as evidence.
>
> On evaluation: 36 generated incidents, half with no culprit deploy, scored by a deterministic scorer. A no-model baseline that blames the latest deploy gets 20%, copying a runbook gets 0%. Gemini got 24 of 30, and I read every miss: some were my scorer being too strict, but the real failure was incidents where a flag or secret changed in another service and the model never looked there. I added a service-listing tool and one generic paragraph, and validated it on three incident types the prompt never mentions.
>
> Around it: pgvector retrieval with its own evaluation, API keys with roles, sessions in Postgres with a lease, MCP in both directions with an untrusted-server policy, OpenTelemetry, containers, and CI. What it isn't: the data sources are simulated and the evaluation is synthetic, so I treat the numbers as comparisons between versions, not production accuracy.

---

## 2. FDE 面试官在找什么 → 你的证据

| 他们看重 | 你的证据（一句话） |
|---|---|
| 把想法做成能上线的东西 | Docker 镜像非 root、优雅停机、生产配置保护、CI、冒烟测试、健康检查（含 worker 心跳） |
| 对 LLM 的不信任（正确的怀疑） | 证据在代码里验证；提示词不可靠这件事有真实记录；MCP 服务端按不可信处理；评论内容中和标记 |
| 评估思维 | 基线（上限、下限）、留出集、配对符号检验、评分器自身的测试、承认评分器调过 |
| 和客户沟通取舍 | 每个决定有"替代方案/为什么/代价"（`docs/architecture.md` 第 5 节） |
| 排查和诊断能力 | CI 红灯时先看日志再改；启动竞态是复现出来的；每个误判都对着模型原话审计 |
| 诚实 | 评分器调过两次、对照实验作废、数据是模拟的、没有真实用户和压测、自托管 GPU 模型还没部署，全部主动说 |

### OpenAI FDE vs Google Cloud FDE III（侧重不同）
- **OpenAI 方向**：强调 evals、提示/工具设计、agent 的可靠性、模型行为。重点讲：§4 故事 1–4、§5.D 评估。
- **Google Cloud 方向**：强调 GCP 落地，**已真实部署**（见 `docs/deployment.md` 第 3 节）：Cloud Run（API、前端）、Cloud Run worker pool（同一个镜像，覆盖启动命令）、Cloud SQL for PostgreSQL + pgvector、Secret Manager、Gemini 通过服务账号调用（不存 key）、Cloud Build。部署后用 `scripts/eval_deployed.py` 验收：36 个场景走真实路径 36/36（留出集 6/6），p50 8.1 秒。诚实说明：模拟数据、没有压测、没有 Cloud Trace 导出、自托管 GPU 模型还没做。队列可以换 Pub/Sub，但说明为什么现在用 Postgres。

---

## 3. 数字速查（都有出处）

| 项目 | 数字 |
|---|---|
| 测试 | 881 后端 + 45 前端；关键代码做过"故意改坏一行"检查；CI 四个任务全绿 |
| 数据集 | 36 个生成的故障场景：30 开发（10 类 × 3 难度，一半没有元凶发布）+ 6 留出（3 种没见过的类型） |
| 基线（30 题，不用模型） | oracle 100%、怪最近发布 20%、抄 runbook 0%、复述工单 0% |
| Gemini 旧配置 v1 | 第一次 22/30（原评分器）；第二次 24/30；有元凶发布 15/15，没有 9/15 |
| 新配置 v2 | 开发集 30/30（评分器调过，偏乐观，p=0.031）；**留出集 0/18 → 18/18**（p=0.031） |
| 机制验证 | 去查关键服务日志：开发集 2/6 → 6/6，留出集 0/18 → 18/18 |
| 校准 | v1 留出集置信度比准确率高 +0.85（"85% 有把握，0% 答对"）；v2 −0.12 |
| 成本 | v2 每次调查约 18.7k 输入 / 0.49k 输出 token，7.3 次模型调用，p50 7.1s / p95 31.4s；比 v1 多约 21% 输入 token |
| 证据校验 | 真实 Gemini 126 份报告，**0 次**被打回 |
| 检索 | 向量 hit@3 1.00 vs BM25 0.80；"几乎没有共同词"的问题 1.00 vs 0.25；混合检索反而更差（0.94） |
| MCP 开销 | 每次调用：内存 12 倍、stdio 66 倍（绝对值很小）；stdio 启动约 0.6 秒 |
| 镜像 | 后端约 296 MB、前端约 313 MB，非 root；停止容器 0.5–0.7 秒退出码 0 |
| 依赖扫描 | pip-audit 和 npm audit：0 个已知漏洞 |

---

## 4. STAR 故事（都是真实发生的）

**故事 1：提示词不可靠 → 代码强制（证据校验）**
- S：agent 报告里的"证据出处"，模型反复写成工具名（如 "search_logs"），提示词里写了规则也没用。
- T：让每条证据都能被工程师追溯，而不是靠模型自觉。
- A：写 `grounding.py`：出处必须逐字出现在某次**成功**的工具返回里；证据类型绑定能提供它的工具（日志证据不能来自工单）；出处不能太短；被拒的报告带着具体原因退回给模型，最多两次，之后整轮失败并回滚。
- R：真实 Gemini 126 份报告 0 次被拒，所以它"没有成本"；故意造的编造证据会被抓住。
- 学到：规则放进代码，可测试、不会被模型绕开。同时清楚它的边界：只证明**出处真实**，不证明**结论正确**。

**故事 2：基线暴露了我自己的评分器有洞**
- S：第一版评分器按关键词判对错。
- A：写了"抄 runbook"这个不用模型的基线，它居然拿了 30%，因为 runbook 里列了好几种原因，包括正确的。
- R：加入"本次事故特有的事实"（只有读过日志才写得出来），降到 0%；还加了测试：任何一份 runbook、工单都不能单独通过。
- 学到：**一个孤立的准确率没有意义，要先有下限和上限**；基线是用来检验题目和评分器的。

**故事 3：评估驱动的改进（最核心的故事）**
- S：Gemini 在"哪次发布出的问题"上 15/15，但"别的服务里记录的变更"（功能开关、密钥轮换）几乎全错。
- T：弄清是模型笨，还是缺能力，再决定怎么改。
- A：逐个对照模型原话审计 8 个答错；发现 4 个是我评分器太严、功能开关 3 个是真的没找到根因；记录模型查过哪些服务的日志，发现从没去查 `config-service`，假设它不知道有这个服务。加 `list_services` 工具和一句通用提示。为防自欺，先做**留出集**（3 种没见过的类型），用**配对符号检验**比较。
- R：留出集 0/18 → 18/18，查别的服务日志的比例 0/18 → 18/18（机制被验证），代价是多约 21% 输入 token。
- 学到：改进要有机制证据，不只是分数变高；留出集防止"背题"。

**故事 4：我自己差点污染留出集，测试抓住了**
- S：写提示词时我顺手写了"扩缩容、DNS、网络策略"，正好是留出集的三种类型。
- A：我之前写了一个测试：提示词里不能出现任何评估场景的服务名或留出类型的词，它失败了。
- R：删掉那几个词，留出集保持干净。
- 学到：对自己也要设防，把"不能作弊"写成测试。

**故事 5：容器化时发现的启动竞态（会悄悄降级）**
- S：给项目 Docker 化，想验证"多个实例同时启动"。
- A：在全新数据库上并发 8 个启动：5 轮 35 个错误；更糟的是知识库建表那一步把错误吞掉，当成"pgvector 不可用"，输掉竞争的实例会一直在**没有语义检索**的状态下运行，没人发现。
- R：加数据库咨询锁（事务级，崩溃自动释放），测试用临时数据库；去掉锁测试就失败。
- 学到：幂等（IF NOT EXISTS）≠ 并发安全；吞错误会把故障变成隐形的降级。

**故事 6：CI 抓到只存在于我机器上的东西**
- S：第一次推到 GitHub，两个任务红灯，本地都是绿的。
- A：在"干净导出的代码"（没有 `.env`、没有生成的文件）里复现：一个测试悄悄依赖我本地 `.env` 的真实模型密钥（没密钥时接口先返回 503，再检查所有权）；前端类型检查依赖 Next 生成的类型。
- R：测试环境里把模型密钥清空（本地和 CI 一致，也保证没有测试能调用真实模型花钱）；加 `typecheck` 脚本；以后推送前都在干净导出里跑一遍 CI。
- 学到："在我机器上能跑"是最危险的话；让测试环境和 CI 完全一致。

**故事 7：CI 又红了，先看日志再改（心跳）**
- S：固定运行环境后，docker 任务连续失败。我最初猜是"优雅关闭 8 秒阈值"。
- A：没改，先让冒烟测试失败时打印容器状态和日志；日志里一行：`worker has no healthcheck configured`。新版 Compose 的 `--wait` 不接受没有健康检查的服务，而我把 worker 的健康检查关了。
- R：这指出真实缺口：worker 卡死没人知道。做了心跳（独立任务更新文件，健康检查看文件新旧）：事件循环被堵死时心跳变旧，worker 在等模型时不变旧；测试里模拟了"堵死事件循环"。
- 学到：我的猜测是错的；**先拿到证据再动手**；让失败自己解释自己。

**故事 8：并发与取消（会话租约）**
- S：会话要在多个实例间共享，同一时刻只能处理一条消息。
- A：租约（原子 UPDATE 抢占并读取最新历史）+ 令牌 + 版本号（围栏）；释放租约放在 shield 保护的范围里，因为被取消的请求里每个 await 都会立刻再被取消，清理发不出去。
- R：崩溃的持有者租约自然过期；过期的慢实例写不进去；有专门的测试（包括在已取消的作用域里释放）。
- 学到：取消会取消清理本身；围栏防止"迟到的写入"。

**故事 9：审批的竞态和 TOCTOU**
- A：批准时要带上审批人看到的**内容哈希**；状态转换是一条原子 `UPDATE ... WHERE status='pending'`；执行前再核对一次哈希；提议者不能批准自己的提议；审计日志用触发器强制只能追加；评论内容把 Jira 的 @提及、链接、宏中和掉，因为内容来自不可信的日志。
- R：两个人同时批准只会发一次；内容被改过会被发现。

**故事 10：作废的对照实验**
- S：想补一个"旧配置 + 12 步"的对照，29/30 报错。
- A：看错误：Gemini 免费额度用完（429）。这个文件不能用。让对比脚本在一次运行出错超过 20% 时明确警告，不再打印一个看起来正常的数字。
- R：报告里如实写"开发集上步数和新工具没有被分开"，并说明留出集上为什么步数不是原因（旧配置平均只用 6.8 次调用，从没碰到 8 的上限）。
- 学到：不能掩盖失败的实验；承认的方式也是能力。

**故事 11：把 agent 真正部署到 Google Cloud 并验收**
- S：项目只在本地和 CI 跑过；Google 的最低要求是在 GCP 上架构 AI 系统，我从没真正用过。
- A：新建专用项目（避免碰到已有 API 密钥的项目）；Cloud SQL（pgvector）、Cloud Run 服务和 worker pool（同一个镜像，覆盖启动命令）、Secret Manager、最小权限服务账号；Gemini 走服务账号，没有模型密钥；知识库导入做成一次性 job；第一把管理员密钥通过 Cloud SQL Auth Proxy 创建，只存进 Secret Manager。部署后先验证安全行为（伪造、篡改、无签名、去重），再写了 `eval_deployed.py` 让 36 个场景走真实的 webhook→队列→worker→模型路径。
- R：36/36，留出集 6/6，p50 8.1 秒、p95 10.9 秒；过程中发现并修好：zsh 变量修饰符写坏连接名、模型只在 global 位置可用、模拟器轮询不带密钥。
- 学到：命令不能照抄文档，要核对云返回的真实值；部署之后要有一个可重复的验收，而不是手点一次。

**故事 12：加第二个模型实现，发现结构化输出的缺口**
- S：想证明抽象层不是空话，并且想知道自托管的开源模型能不能驱动这个 agent。
- A：写 OpenAI 兼容适配器（循环一行不改），本地 Ollama 跑 Gemma 4 的留出集；第一次 9/18，全部是步数用完；追踪发现它把报告字段写错；原因是 `$ref` 没被服务器展开；在适配器里展开引用。
- R：格式错误 25 → 0；准确率 9/18 → 13/18（6 个场景，不显著）；剩下的瓶颈是逐字引用证据；延迟 p50 约 88 秒对 Gemini 的 8 秒。
- 学到：先取证据再动手（一次追踪胜过猜测）；厂商差异应该被适配器吸收；对比要同时看成本、速度和失败的性质。

**加分：改坏一行测试（变异检查）**：对安全相关代码逐行改坏，看有没有测试失败。多次发现"第一次就通过"的测试其实很弱（比如只测了极端情况），补强后才算数。

---

## 5. 技术问答

### A. Agent 设计
**为什么手写循环，不用 LangGraph？**
要点：每一步可见可控。 
> The loop is small, and it is exactly where I need hooks: the grounding check on submit, rollback of history on failure, step limits, tracing and approval. I kept an SDK function-calling variant to compare, so I know what the framework hides: the loop becomes a black box and I can't intercept a call before it runs.

**怎么限制成本和死循环？**
> Per-turn step limit, per-session turn limit, a cap on tool result size, bounded grounding retries, a per-job timeout in the worker. History is re-sent every call, so input tokens dominate cost, about 30–40x the output.

**模型输出怎么变成结构化报告？**
> The final answer is a tool call to `submit_rca_report` whose arguments are a Pydantic model. If validation fails, the error goes back to the model to fix. A hypothesis without evidence can't even be constructed.

**工具是模型调用的，怎么防止它调用不该调用的？**
> It only emits a request. The registry has an allowlist, validates arguments, and caps results. The agent has no write tools at all.

### B. 可靠性
**Postgres 当队列？怎么保证不重复领任务？**
> `FOR UPDATE SKIP LOCKED` lets many workers claim different rows. Completion and failure are fenced by worker id and attempt number, so a worker whose lease expired can't overwrite the new one's result. Failures back off exponentially (base 10 s, capped at 5 min), bad data fails permanently, and an idempotency key makes webhook redelivery harmless.

**为什么不用 Kafka/Pub/Sub？**
> Jobs have to be durable and queryable anyway, volume is a few per minute, and one fewer component is easier to operate. I'd switch when throughput grows by orders of magnitude. On GCP, Pub/Sub is the natural replacement.

**Webhook 怎么做的安全？**
> HMAC-SHA256 over the raw body with constant-time comparison, fail closed when no secret is set, a size limit, and status codes chosen for Jira's retry rules: 202 queued, 200 duplicate or ignored, 401 bad signature, 503 so Jira retries.

**会话怎么做到多实例共享？**
> State lives in Postgres; a message first takes a lease with a token in one atomic statement that also returns the latest history; saves must show the token and the expected version. A crashed holder's lease just expires.

### C. 安全
**Prompt injection 怎么办？**
> I don't claim to prevent the model from being influenced. I limit what influence can do: the agent is read-only, claims need verbatim evidence, the comment is rendered from a fixed template with markup neutralized, and a human approves the exact text. Everything the model reads is treated as untrusted.

**API key 为什么用 SHA-256 不用 bcrypt？**
> bcrypt is for low-entropy human passwords. My secret is 256 random bits, brute force is infeasible, and a slow hash would only slow every request. I also compare in constant time and do the same work when the key id doesn't exist, so timing doesn't reveal which ids exist.

**MCP 的风险？**
> The client treats any server as untrusted: allowlist of tool names, read-only requirement, truncated descriptions (tool poisoning), size and time limits, no sampling callbacks. The source of tools is server configuration; a request can't choose it, or anyone could make me spawn a subprocess per request.

### D. 评估
**怎么评估 agent？**
> Four layers: a dataset built so good and bad agents score differently, a deterministic scorer that is itself tested, no-model baselines for floor and ceiling, and a held-out set plus paired comparison for changes. Real-model results are always read next to the baselines.

**为什么不用 LLM 当裁判？**
> Slow, costly, not reproducible, and biased toward well-written answers. Keywords are crude but explainable, and I test the scorer. I'd add an LLM judge as a second opinion on disputed cases, not as the primary.

**为什么用符号检验？**
> 30 noisy runs: two accuracy percentages say little. Pairing by scenario and counting newly-correct vs newly-wrong ignores ties. If the change did nothing, flips go either way equally, so the chance of seeing 6–0 is 2 × (1/2)^6 ≈ 0.031. It is exact and needs no distribution assumptions.

**为什么需要留出集？**
> I tuned the prompt while looking at the development set. The held-out types weren't in the data I looked at and aren't named in the prompt, which a test enforces. If the gain were memorization, it would vanish there.

### E. 检索
**为什么只用向量，不做混合？**
> I measured it: hybrid rank fusion was worse than pure vector on this corpus (hit@3 0.94 vs 1.00). Caveat: 35 author-labeled queries and a short corpus, so I don't over-generalize.

**HNSW + 过滤的坑？**
> Any WHERE clause in the inner query makes the planner skip the index. So the inner query is a pure nearest-neighbour search and filters apply outside (post-filtering), with an EXPLAIN test proving the index is used.

### F. 运维
**怎么保证可观测，又不泄露数据？**
> OpenTelemetry spans carry metadata only: tool names, counts, token numbers, statuses. Every attribute is sanitized to numbers, booleans and short strings, and the session id, which is a bearer-style capability, is only recorded as a hash prefix. Logs carry the trace id.

**怎么部署到 GCP？**
> I deployed it on Google Cloud: the API and frontend as Cloud Run services, the worker as a Cloud Run worker pool running the same image with a different start command, Cloud SQL for PostgreSQL with pgvector, Secret Manager, and Gemini reached through the service account, so there is no model key anywhere. Afterwards I ran all 36 evaluation scenarios through the real webhook, queue, worker and model path: all correct, 8 seconds median. What I haven't done: a load test, trace export to Cloud Trace, database backups and high availability, and a self-hosted GPU model. One real decision: the worker has no HTTP port and polls the database queue, so it has to be an always-on worker pool instance; with push delivery (Pub/Sub or Cloud Tasks) it could scale to zero.

---

### G. 自托管开源模型

**部署一个开源模型需要什么？**
一个开源模型在 Hugging Face 上是一个仓库，部署它需要三类东西：
- **Weights（权重，`*.safetensors`）：**模型学到的知识，体积最大（Gemma 4 26B 的 bf16 权重约 52 GB），决定要多大的显存。
- **`config.json`：**模型的骨架，层数、隐藏层大小、注意力头数、上下文长度；推理引擎靠它搭出网络，权重才能放进去。
- **`tokenizer.json`（连同聊天模板）：**语言编码规则，把文字切成 token 编号再变回文字，要和训练时完全一致，否则输出是乱码；聊天模板决定怎么把系统提示、用户消息和工具调用拼成模型认识的格式。
> A model repository has three essential parts. **Weights** (`*.safetensors`) are the learned parameters, the "brain", and by far the biggest part (about 52 GB in bf16 for Gemma 4 26B). **`config.json`** is the architecture: layer count, hidden size, attention heads, context length, so the runtime knows how to build the network the weights fit into. **`tokenizer.json`** (with the chat template) is the language encoding: how text becomes token ids and back, which must match training exactly or the model gets garbage. An inference server such as vLLM loads all three, and serves them behind an API.

**Hugging Face、Ollama、vLLM、GCP 各是什么角色？**

| 名称 | 角色 | 我们的用法 |
|---|---|---|
| Hugging Face | 存放模型三件套的仓库（类似 Maven Central） | Gemma 4 的权重、`config.json`、`tokenizer.json` |
| Ollama | 本地运行模型的工具；把模型打成 GGUF（权重、分词规则和元数据放进一个量化后的单文件） | 本地跑 `gemma4:26b`（约 16 GB，量化过，所以笔记本能跑） |
| vLLM | 面向生产的推理服务器，直接读 Hugging Face 格式，高并发、前缀缓存、工具调用解析 | 计划部署在 Cloud Run GPU 上，用 bf16 原始权重 |
| GCP（Cloud Run GPU） | 提供 GPU、按用量计费、可缩到零、用 IAM 保护接口 | 计划的部署目标；还没做 |
| OpenAI 兼容接口 | 事实标准的聊天接口，Ollama、vLLM、OpenAI 都支持 | 我们的适配器对接它，本地和云上用同一个适配器 |

**部署的是开源模型，为什么用 OpenAI 的接口？**
> "OpenAI-compatible" does not mean I call OpenAI. Open weights are just files; they have no API of their own. The serving software (Ollama, vLLM) provides the API, and most of them copied the shape of OpenAI's chat-completions JSON, which became the de facto standard, like S3-compatible storage or the SQL standard. So one adapter works against a local Ollama, a vLLM server on Cloud Run, or OpenAI itself, and only the base URL and model name change. Nothing is sent to OpenAI.

要点：接口是**服务软件**提供的，不是模型提供的；开源模型自己没有"官方接口"，Ollama 也有自己的原生接口，vLLM 也有，但大家都额外兼容了 OpenAI 的格式，所以选它，换服务器不用改代码。

**本地和云上的结果能直接对比吗？**
不能直接当成同一个模型：本地 Ollama 用的是量化后的 GGUF（约 16 GB），vLLM 加载的是 bf16 原始权重（约 52 GB），精度略有差别，速度也受硬件影响（笔记本对 GPU）。对比时要说明这一点。

## 6. 难问题 / 诚实的弱点

**"数据是模拟的，为什么该相信它？"**
> Fair. The tool back ends are simulated, so I don't claim real-world accuracy. What carries over: the agent loop, the trust and safety mechanisms, and the evaluation method are built against the tool interface, so only the connectors change. And the findings that mattered (the model not looking in other services, the scorer's holes) are about behavior, which is why they're still useful.

**"评分器是你看了结果后调的，这不算作弊吗？"**
> I adjusted it twice after seeing outputs, with a stated rule, I froze the real answers as test cases, and I re-scored both sides of a comparison with the same rule. But yes, the development numbers are optimistic, and I say so in the report. The held-out scorer was written before any run and never touched, so that result is the one to trust.

**"0/18 到 18/18 太完美了吧？"**
> It's a large effect because the capability was binary: v1 never looked in the other service at all (0 of 18), so it could not succeed. I verified that the mechanism changed (looking at the right logs 18/18), not just the score. The limits are six scenarios from one generator, and I'd want real incidents next.

**"证据校验不是万能的吧？"**
> Right: it proves provenance, not correctness. A real log line can support a wrong conclusion. That's what the end-to-end evaluation measures; the two are complementary.

**"p95 是 31 秒，能接受吗？"**
> For an automatic first pass that posts to a ticket, yes; for interactive chat the streamed steps make the wait visible. The tail is mostly model latency and rate limits on the free tier. I'd look at caching tool results and parallel tool calls next.

**"你没测什么？"**
> Load, real connectors, trace export to Cloud Trace, and an LLM judge validated on the real model. I also didn't test other model families, and the free tier limits how many repeats I could afford.

**"重来你会改什么？"**
> Build the held-out set and the baselines first, before tuning anything, and run CI from day one: both the `.env` dependency and the lockfile problem would have shown up in the first week instead of at the end.

---

## 7. 如果继续做（可以直接说）
1. 接真实的 Jira、日志（如 Cloud Logging）、Git/CI 连接器，每个只读凭证。
2. ~~Vertex AI 上的 Gemini~~（已完成：服务账号身份，部署在 Google Cloud 上）；自托管的开源模型放到 Cloud Run GPU（适配器已有，部署还没做）。
3. 用真实历史事故建评估集；LLM 裁判作为二次意见；更多模型对比。
4. 工具结果缓存与并行工具调用，降低延迟和 token。
5. Cookie 会话 + SSO；Alembic 迁移；审计表的只插入数据库角色；限流；压测；镜像扫描、固定摘要。
6. 评估放进 CI（每个 PR 自动跑基线和一个小的真实模型子集）。

---

## 8. 练习清单
- [ ] 不看稿，2 分钟讲清架构图（先讲两个入口、一个 agent、数据库在中间、人在最后）。
- [ ] 白板画"一个 Jira 事件到评论发出"的时序（webhook → 队列 → worker → 校验 → 提议 → 审批）。
- [ ] 讲 3 个故事（建议 3、1、7），每个 2 分钟，控制在"情境一句、做了什么、结果数字、学到什么"。
- [ ] 回答 §6 的每个难问题，**第一句承认局限**。
- [ ] 过一遍 `docs/demo-script.md` 现场演示一次，计时 5 分钟。
