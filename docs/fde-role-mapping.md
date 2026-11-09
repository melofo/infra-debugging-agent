# 岗位要求对照与面试材料（FDE 方向）

> 面向 Forward Deployed Engineer / 生成式 AI 部署类岗位的准备材料：把岗位要求逐条对照到这个项目的证据，标出缺口，附上项目的简历条目、交付方法和问答。项目细节见 `docs/architecture.md`、`docs/evaluation-report.md`、`docs/deployment.md`；故事和技术问答见 `docs/interview-prep.md`。

## 1. 一句话定位

> Backend/platform engineer 转向生产级 agent 系统：**能评估、可观测、安全、并且诚实地说明数字意味着什么**。

两个岗位的共同核心都是：**把前沿模型变成客户环境里真正上线、可度量的系统**，并把现场发现反馈给产品。这个项目就是在小尺度上走完这条路。

---

## 2. 两个职位真正看重什么（按重要性排序）→ 你的证据

评级：**强** = 项目里有可展示的代码和数字；**部分** = 有但不完整；**⚠ 空白** = 没有。

### Google Cloud FDE III, GenAI（招聘原文里的关键词）

| # | 他们要什么（原文） | 你的证据 | 评级 |
|---|---|---|---|
| 1 | 从原型到**生产级 agentic workflow**，含 **MCP servers** | 手写 tool-calling 循环；MCP 服务端（stdio + 带认证的 Streamable HTTP）和 MCP 客户端（把服务端当不可信方：白名单、只读、大小/时间限制）；729+45 个测试、CI、容器 | 强 |
| 2 | **评估流水线和可观测性**（准确度、安全、延迟） | 36 个生成场景 + 确定性评分器 + 无模型基线 + 留出集 + 配对符号检验；OpenTelemetry 追踪和指标（不含内容）| **强（最强项）** |
| 3 | **RAG / 向量库**，结构化和非结构化数据 | pgvector + HNSW，runbook 和历史事故分块入库（幂等、事务性切换）；检索评估 35 条查询 | 强 |
| 4 | **LLM 原生指标**（tokens/sec、cost-per-request）、**状态管理**、**精细追踪** | 每次调查的 token、调用次数、p50/p95 延迟、成本估算开关；会话状态存 Postgres（租约+版本号）；span 树 job→turn→模型调用→工具调用 | 强（tokens/sec 没单独量） |
| 5 | **在 GCP 上架构 AI 系统**（最低要求） | **已真实部署**：Cloud Run（API、前端）、Cloud Run worker pool、Cloud SQL（PostgreSQL 17 + pgvector）、Secret Manager、Artifact Registry、Cloud Build；Gemini 通过服务账号调用（没有任何 API 密钥）；部署后用 36 个场景走真实的 webhook→队列→worker→模型路径验证（36/36，中位数约 8 秒）。**限制：**个人项目、模拟数据、没有客户；自托管 GPU 模型和 Cloud Trace 还没做 | **强（个人项目，非客户）** |
| 6 | **多 agent 框架**（LangGraph、CrewAI、ADK；ReAct、自我反思、分层委派）— 加分项 | 我们的循环就是 ReAct；"报告被拒→带原因重写"类似自我反思；**已实现路由、串联、并行三种工作流**（`app/agent/workflows.py`，22 个测试，假模型验证，**没有用真实模型评估**）；专家是同一个 agent 加不同侧重点，**不是**用 ADK/LangGraph 做的 | 部分（模式有，框架没用过） |
| 7 | **带领客户技术发现会议**（最低要求）、与客户共建 | 项目里没有（项目没有真实客户） | **⚠ 空白（见 §4）** |
| 8 | 把现场模式沉淀成**可复用模块/给产品提需求** | grounding、eval 框架、MCP 客户端策略都是可复用模块；评估发现可转成产品反馈（见 §5） | 部分 |

### OpenAI FDE（招聘原文里的关键词）

| # | 他们要什么（原文） | 你的证据 | 评级 |
|---|---|---|---|
| 1 | **端到端负责**：发现、范围、设计、构建、上线 | 项目覆盖设计→构建→评估→容器化→CI；发现和客户采纳是空白 | 部分 |
| 2 | **全栈**生产代码（Python/JS） | FastAPI 后端 + Next.js 前端（聊天、任务页、审批页） | 强 |
| 3 | 理解**模型行为如何影响产品**；**eval 驱动的反馈**去改变产品/模型路线 | 评估发现：模型停在表面原因、不去查别的服务；旧配置"85% 把握、0% 答对"（校准 +0.85）；用对照实验验证改进 | **强（最强项）** |
| 4 | 在范围、速度、质量间**取舍**，保护交付 | 每个决定有"替代方案/为什么/代价"表；对照实验作废时如实写进报告 | 强 |
| 5 | **沉淀成工具、手册、构件** | 评估框架、冒烟测试、MCP 客户端策略、`docs/` 手册 | 部分 |
| 6 | 清晰沟通、早发现风险、高压下保持冷静 | CI 红灯时"先拿日志再改"的故事（§5） | 靠故事 |
| 7 | 5+ 年工程/部署经验，含面向客户的工作 | 取决于个人经历，项目本身证明不了年限 | 面向客户的部分靠 §6 的交付方法和真实故事 |

---

## 3. 项目的简历条目（三条）

规则：评估过的写成结果，没评估的只写 implemented。放在单独的 Projects 栏。

- Built and deployed an end-to-end incident-investigation agent (Python, FastAPI, Next.js, PostgreSQL/pgvector) on Google Cloud (Cloud Run services and a worker pool, Cloud SQL, Secret Manager, Cloud Build), triggered by Jira webhooks or a chat UI with persistent memory and live step streaming; Gemini is called through a service account with no API keys, and a tool-calling loop over logs, deploys, runbooks and a vector knowledge base returns schema-validated reports with **evidence grounding enforced in code**, human-approved write-back and an append-only audit log.
- Designed a **provider-agnostic LLM layer** (adapters for Gemini and any OpenAI-compatible server) that left the agent loop, safety checks and evaluation unchanged when a locally hosted open model (Gemma 4) was added; diagnosing that model's structured-output failures inside the adapter eliminated every format rejection. Also exposed the tools through an **MCP server**, added routing, chaining and parallel specialist workflows, and built it on a Postgres job queue, API-key roles, **OpenTelemetry** and a real-database CI pipeline.
- Designed an **evaluation pipeline** (generated scenarios, deterministic scorer plus an LLM-judge second opinion, no-model baselines at **0–20%** vs **100%** oracle, held-out set, paired significance tests): a service-listing tool and one system-prompt paragraph took held-out accuracy from **0% to 100%** for about **21% more input tokens**; the same scenarios through the deployed webhook-to-worker path scored **100% Top-1 at ~8 s median**; pgvector RAG beat BM25 on hit@3 (**100% vs 80%**), and hybrid fusion measured worse and was not shipped.

**没写进条目的（还没评估）：** v3 提示词（XML 标签加示例）、重排序、扩展思考的效果、工作流是否比单 agent 更准、缓存省了多少。被问到答 "implemented and tested, not yet evaluated"。LLM 裁判只实现了，没有在真实模型上验证，一页放不下就先删它。

**可以写：** deployed on Google Cloud（真的做了）。**仍然不要写：**自托管模型已部署到 GPU（还没做）、支持 Claude。36/36 是部署验收：每个场景只跑一次、数据是合成的、开发集评分器调过，可信的是留出集 6/6，被问到主动说。

## 4. 空白，以及怎么补（要诚实）

| 空白 | 为什么重要 | 补法 / 现在怎么说 |
|---|---|---|
| ~~没有真正用 GCP~~ **已补上**（Google 最低要求） | 招聘原文第一条资格是在云平台（如 GCP）上架构 AI 系统 | 已真实部署并验证，细节见 §10。**剩下的缺口：**自托管 GPU 模型（故意放最后，贵）、追踪导出到 Cloud Trace、压力测试。说法：*"I deployed it on Cloud Run and Cloud SQL with Gemini through a service account and measured it end to end; the remaining gaps are a self-hosted GPU model, trace export and load testing."* |
| **没有多 agent 框架经验**（加分项） | JD 点名 LangGraph / CrewAI / ADK | 两个选择：(a) 做一个小对比：用 ADK 或 LangGraph 实现同一个任务，写 1 页对比；(b) 准备好立场：*"I chose a single agent on purpose; my loop implements ReAct, the grounding retry is a form of reflection, and I'd split into agents only when contexts or permissions need separating."* |
| **没有"带领客户技术发现会议"**（Google 最低要求）| 项目是作品集，没有真实客户 | 用自己经历里和干系人沟通的**真实**例子（项目补不了这一项）；同时准备好 §6 的发现问题清单，面试官问"你会怎么做发现"时直接用 |
| 数据源是模拟的，没有真实用户 | 面试官一定会问 | 主动说：架构和评估方法对工具接口成立，换的只是连接器；评估是合成的，所以只拿来比较版本，不宣称真实准确率 |
| 没有压测、没有真实 GCP 部署、没有 LLM 裁判 | 同上 | 写进"如果继续做"，不回避 |

---

## 5. 每个职位带哪 3 个故事（详细版见 `docs/interview-prep.md`）

**Google 版**（强调：生产化、评估、MCP、GCP 设计）
1. **评估驱动的改进**：Gemini 在"别的服务里的变更"上几乎全错 → 发现它从不去查 `config-service` → 加服务目录工具 + 通用提示 → 留出集 **0/18 → 18/18**，机制得到验证（去查关键服务日志 0/18 → 18/18），代价 +21% 输入 token。
2. **提示词不可靠 → 代码强制**：模型把工具名当证据 → 证据校验（逐字出现在成功的工具返回里、类型绑定工具）→ 126 份真实报告 0 次被拒，MCP 服务端按不可信处理。
3. **容器化发现的启动竞态**：8 个实例同时启动，5 轮 35 个错误，更糟的是**悄悄**关掉了语义检索 → 数据库咨询锁 + 并发测试。（对应 Google 的"生产成熟度"）

**OpenAI 版**（强调：模型行为、eval 反馈、取舍、冷静）
1. **同上的评估故事**，重点放在**给 Research/Product 的现场反馈**：模型停在表面原因、不主动探索相关服务；旧配置**校准差距 +0.85**（85% 把握、0% 答对），这就是"模型在缺少能力的地方会自信地错"。
2. **基线暴露自己的评分器有洞**：抄 runbook 拿了 30%，说明评分太松 → 加"本次事故特有事实"→ 0%。展示"先问数字可不可信"。
3. **CI 红灯，先拿证据再动手**：docker 任务连续失败，我最初猜是"关闭超时阈值"，没改，先让失败自己打印日志，日志一行 `worker has no healthcheck` → 真实缺口（卡死的 worker 没人知道）→ 心跳。（对应"高压下保持冷静和判断力"）

每个故事 90 秒：情境一句、你做的决定、数字结果、学到什么。

---

## 6. 我怎么做一次客户交付（FDE 手册，用来回答任何"你会怎么做"的问题）

**发现 → 范围 → 设计 → 薄切片 + 评估 → 加固 → 上线与采纳 → 反馈产品。** 用这个项目当例子：

| 阶段 | 做什么 | 这个项目里的对应 |
|---|---|---|
| 发现 | 问题、使用者、**成功指标**、数据是否就绪、权限和安全边界 | 值班排查；成功 = Top-1 找对根因、证据可追溯、不越权写入 |
| 范围 | 最小的有价值切片，明确不做什么 | 只读 + 一个受审批的写入；模拟数据先行 |
| 设计 | 信任边界，**先设计评估** | 一切模型读到的都不可信；先有基线和评分器 |
| 薄切片 | 端到端跑通，带评估 | Jira → 队列 → agent → 报告 → 审批 |
| 加固 | 可靠性、可观测、安全 | 租约/围栏、OpenTelemetry、API key、审计日志 |
| 上线采纳 | 灰度、培训、成本控制 | 步数/轮数限制；`AGENT_VARIANT` 可回退；v1/v2 对照 |
| 反馈 | 现场发现 → 产品/模型 | 评估发现（见 §5 故事 1） |

### 发现会议问题清单（回答"你会问客户什么"）
1. 现在这个流程谁在做、一次花多久、一个月多少次？（量化价值）
2. "做对了"长什么样？有没有历史上已经确认过根因的案例可以当评估集？
3. 数据在哪里：日志、工单、发布系统？访问方式、权限、延迟、保留期？**数据准备度**通常是第一个阻塞点。
4. 安全边界：模型能看到什么、**绝不能**碰什么？写操作谁审批？合规（数据驻留、审计）要求？
5. 错了的代价：误报 vs 漏报哪个更糟？需要人工复核到什么程度？
6. 谁会用、怎么融入现有工具（Jira、Slack、值班系统）？谁是推动采纳的人？
7. 延迟和成本预算：可接受多久、每次调查多少钱？
8. 现有工具/团队技能：能不能复用他们的日志平台、身份系统？
9. 上线方式：先影子模式（只提议不执行）？成功标准和退出条件？
10. 谁来维护：交接后客户团队能不能自己改评估集和提示？

### 我会早期盯的风险
数据质量与权限、评估集太少或太干净、模型在"没见过的类型"上自信地错、成本随历史长度增长、写操作的越权、没有人负责采纳。

---

## 7. 数字速查（只记这 10 个）

| 数字 | 含义 |
|---|---|
| 36 / 30+6 | 评估场景数（开发 30 + 留出 6），一半没有元凶发布 |
| 100 / 20 / 0 / 0 % | oracle / 怪最近发布 / 抄 runbook / 复述工单 |
| 24/30 | 旧配置（v1）开发集；有元凶发布 15/15，没有 9/15 |
| **0/18 → 18/18** | 留出集 v1 → v2（p = 0.031） |
| 0/18 → 18/18 | 去查关键服务日志的比例（机制验证） |
| **+0.85** | 旧配置在留出集上的校准差距（85% 把握，0% 答对） |
| +21% | v2 比 v1 多的输入 token |
| 0 / 126 | 真实报告里被证据校验拒绝的次数 |
| 7.1 s / 31.4 s | v2 每次调查 p50 / p95 延迟 |
| 729 + 45 | 后端 + 前端测试数；CI 四个任务全绿 |

---

## 8. 最常被问到的问题（模板；个人经历部分请用真实例子填）

1. **Why FDE / why this role?** [用你自己的真实动机。可以连接：这个项目就是为了练习把模型变成客户环境里真正可用的系统这条路。]
2. **Walk me through a project you're proud of.** → §5 故事 1，90 秒，先结果后过程。
3. **Tell me about a time you disagreed with a customer / stakeholder.** [填一个自己经历里的真实例子，项目补不了这一项。]
4. **Biggest failure?** → 作废的对照实验（额度用完）+ 评分器调过两次：说你怎么发现、怎么如实披露、怎么改流程。
5. **How would you scope a 3-month GenAI deployment?** → §6 手册，强调先做评估集和基线。
6. **What would break in production?** → 数据质量、成本随历史增长、模型在新类型上自信地错、写操作越权；对应的缓解措施都在项目里。
7. **Design an agent for customer X's workflow.**（系统设计题）→ 用 `docs/architecture.md` 的结构：入口 → 队列 → agent → 工具（权限）→ 校验 → 人工审批 → 评估与可观测；10 分钟内画完，最后讲取舍。
8. **How do you know it works?** → 基线（上下界）→ 留出集 → 配对检验 → 承认局限。

---

## 9. 接下来的行动清单（按价值排序）

1. ~~补 GCP~~ **已完成**（见 §10）。剩下：自托管 GPU 模型（最后做，贵）；然后**删除整个 GCP 项目**，避免持续计费（Cloud SQL 和 worker 约每天 $3）。
2. **练 §5 的 6 个故事**，每个 90 秒；再过一遍 `docs/demo-script.md` 现场演示，计时 5 分钟。
3. **准备 2 个真实的"客户/干系人"故事**（§8 第 3 题等），这是项目补不了的。
4. （可选）做 ADK 或 LangGraph 的小对比，补多 agent 这个加分项。
5. （可选）等 Gemini 额度恢复后补跑"旧配置 + 12 步"的对照，彻底排除步数的影响。

---

## 10. GCP 部署：面试用的故事、数字和被追问的答案（2026-11 真实执行）

**一句话**：我把 agent 真正部署到 Google Cloud，并且在部署出来的系统上用同一套评估集做了验收。

**架构（能在白板上画）**：浏览器 → Cloud Run 前端 → Cloud Run API → Cloud SQL（任务队列、会话、审计、知识库向量表）；worker 是单独的 Cloud Run worker pool（同一个镜像，覆盖启动命令），自己轮询数据库领任务；Gemini 通过服务账号调用，没有任何模型密钥；密钥在 Secret Manager，一个最小权限的服务账号。

**数字（都有出处，见仓库 `docs/deployment.md` 第 3 节和 `docs/evaluation-report.md` 5.7）**
- 部署后跑 36 个场景走真实路径：36/36，留出集 6/6，没有错误；平均 7 次模型调用，约 1.6 万输入 / 480 输出 token；从发 webhook 到出结果中位数 8.1 秒、p95 10.9 秒。
- 持续成本（估算）：Cloud SQL 约 $1/天，常驻 worker 约 $1.7/天，其余缩到零；预算提醒不会限制花费，清理是删除整个项目。

**故事 A：部署文档里的命令不能照抄（我自己踩的坑）**
- zsh 里 `$PROJECT:us-central1:db` 的 `:u` 会把变量转大写，数据库连接名在一个密钥里被悄悄写坏；我在部署时遇到错误信息才发现，修正密钥并改用 `${PROJECT}`，最后用 `gcloud sql instances describe` 返回的连接名核对。
- Gemini 的新模型只在 `global` 位置能调用，`us-central1` 返回 404；我部署前先用一个极小的请求验证，没有等到部署完才发现。

**故事 B：加第二个模型实现，循环一行不改**
- 新增一个 OpenAI 兼容适配器，agent 循环、工具、证据核对、评估都不动；用一个本地开源模型（Gemma 4，Ollama）跑留出集：第一次 9/18，**9 次全是步数用完，不是答错**；一次追踪发现它不会填我们的报告格式；原因是 JSON Schema 里的 `$ref` 引用服务器没有展开，模型看不到证据的字段；在适配器里展开后，格式错误 25 → 0，准确率 9/18 → 13/18（样本小，不显著），剩下的瓶颈是逐字引用证据。
- 这也是给产品和研究的现场反馈：小模型理解事故，但结构化输出的契约要重点测。

**被追问的答案（诚实版）**
- *知识库部署在哪？* 就在同一个 Cloud SQL 实例里：两张表，向量列 768 维加 HNSW 索引；导入是一次性的 Cloud Run job（切成 49 块、调用嵌入模型、写表）。
- *worker 为什么常驻？* 我用 Postgres 当队列、轮询领任务（任务量小、要持久可查询）；代价是 worker 不能缩到零。任务量大几个数量级，或者空闲成本重要，就改成 Pub/Sub 或 Cloud Tasks 推送。
- *API 和 worker 为什么不拆成两个镜像？* 它们共用数据模型和 agent 代码，一个镜像两个启动命令，避免版本不一致；依赖差别大或团队分开时才拆。
- *这是生产吗？* 不是：模拟数据、没有真实用户、没有压测、数据库单可用区无备份、没有 Cloud Trace。是一个完整的、可验证的部署，不是客户上线。
- *你自己托管过大模型吗？* 本地用 Ollama 跑过并评估；云上的 GPU 部署还没做（贵，放最后）。

**还没做（别说成做了）**：自托管 GPU 模型、Cloud Trace 导出、压力测试、Cloud Armor、部署流水线、数据库高可用和备份。
