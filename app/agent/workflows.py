"""三种最基本的"工作流"模式：路由（routing）、并行（parallelization）、串联（chaining）。

先分清两个词（Anthropic 的 "Building effective agents" 里的划分）：
  工作流 workflow：步骤和分支由【我们的代码】写死，模型只在每一步里干活。
  agent：下一步做什么由【模型自己】决定（我们的 loop.py 就是）。
这个文件把"写死的流程"搭在现有 agent 外面，不改 loop.py：每个"专家"就是同一个 InvestigationAgent，
工具和规则完全一样（证据校验、只读、步数上限都照旧），只是系统提示词多一句"先看哪一类变更"。

  路由 routing         先用一次便宜的模型调用给工单分类，再选对应的专家。类似 Java 里按类型选择策略（Strategy）。
  并行 parallelization 几个专家【同时】查，最后合并成一份报告。类似 CompletableFuture.allOf 再合并结果。
  串联 chaining        一步的输出是下一步的输入，步骤之间可以放"关卡"（gate）：这里是
                       路由 -> 一位专家 -> 关卡（信心不够就升级成全部专家并行）。便宜的路径先走，贵的只在没把握时才走。

诚实的局限：
  - 关卡用的是模型自己报的置信度。旧配置 v1 在留出集上置信度比准确率高 0.85（见 docs/evaluation-report.md），
    所以这个关卡只对校准较好的 v2 才有意义；它是"升级的信号"，不是正确性的证明。
  - 这些模式还没有用真实模型评估过（测试用的是假模型）；不要声称它们比单个 agent 更准。
  - 专家提示词刻意写得很泛（不点名具体的故障类型），避免污染留出集（见 tests/test_service_discovery.py 的守卫测试）。
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from app.agent.common import SYSTEM_PROMPT_V2, AgentError, InvestigationResult
from app.agent.loop import InvestigationAgent
from app.agent.tools import ToolRegistry
from app.llm.base import LLMClient, LLMError
from app.schemas import RCAReport

# 专家 = 同一个提示词 + 一句"先看哪里"。键就是路由的标签。
SPECIALISTS = {
    "deploy": "Focus first on recent deploys and code changes of the affected service.",
    "config": "Focus first on configuration, feature flags, credentials and certificates.",
    "infrastructure": "Focus first on infrastructure and traffic changes.",
}
GENERAL = "general"  # 分类不出来（或模型回了奇怪的东西）时用：不加侧重点，就是普通的 v2 agent
ROUTES = (*SPECIALISTS, GENERAL)

ROUTER_SYSTEM = (
    "You classify incident tickets by the most likely kind of cause. "
    f"Answer with exactly one word from: {', '.join(ROUTES)}."
)

# 关卡：最可能的根因的置信度低于它，就认为"这条路径没把握"，升级成全部专家并行。
LOW_CONFIDENCE = 0.6

# 一个"造 registry 的函数"：每位专家用自己的一份工具集，互不共享状态（类似 Java 的 Supplier<ToolRegistry>）。
RegistryFactory = Callable[[], ToolRegistry]


@dataclass
class WorkflowResult:
    report: RCAReport
    routes: list[str]  # 实际跑过哪些专家（按顺序）
    escalated: bool = False  # 串联里，关卡是否触发了升级
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    notes: list[str] = field(default_factory=list)


def specialist_prompt(route: str) -> str:
    focus = SPECIALISTS.get(route)
    return SYSTEM_PROMPT_V2 + (f"\n\n{focus}" if focus else "")


# ---------------------------------------------------------------- 路由
async def route_ticket(llm: LLMClient, ticket_text: str) -> tuple[str, int, int]:
    """给工单分类。返回 (标签, 输入 token, 输出 token)。

    这是一道"关卡"：只接受预先列好的标签，其他任何回答都退回 general。
    永远不要把模型的原话直接当成要执行的东西；这里的标签只会用来【选择】一份提示词，不会被拼进任何命令。
    分类失败（模型出错）也不拖垮整个调查：退回 general。
    """
    try:
        response = await llm.generate(ticket_text, system=ROUTER_SYSTEM)
    except LLMError:
        return GENERAL, 0, 0
    words = response.text.strip().lower().split()
    label = words[0].strip(".,:;\"'*`") if words else ""
    return (label if label in ROUTES else GENERAL), response.input_tokens, response.output_tokens


async def _run_specialist(
    llm: LLMClient, make_registry: RegistryFactory, route: str, ticket_id: str, max_steps: int
) -> InvestigationResult:
    agent = InvestigationAgent(llm, make_registry(), max_steps=max_steps, system_prompt=specialist_prompt(route))
    return await agent.investigate(ticket_id)


async def investigate_routed(
    llm: LLMClient, make_registry: RegistryFactory, ticket_id: str, ticket_text: str, max_steps: int = 12
) -> WorkflowResult:
    """路由：分类一次，然后只跑被选中的那一位专家。"""
    route, tokens_in, tokens_out = await route_ticket(llm, ticket_text)
    result = await _run_specialist(llm, make_registry, route, ticket_id, max_steps)
    assert result.report is not None  # investigate() 保证有报告
    return WorkflowResult(
        report=result.report,
        routes=[route],
        llm_calls=result.llm_calls + 1,
        input_tokens=result.input_tokens + tokens_in,
        output_tokens=result.output_tokens + tokens_out,
    )


# ---------------------------------------------------------------- 并行
def merge_reports(ticket_id: str, reports: list[RCAReport]) -> RCAReport:
    """把几份报告合成一份：所有假设放在一起按置信度排序，最多留 5 个。

    每份报告里的证据都已经单独通过了证据校验（在各自的 loop 里），合并不会引入没验证过的证据。
    摘要用最有把握的那份报告的。
    """
    best = max(reports, key=lambda r: r.hypotheses[0].confidence)
    hypotheses = sorted((h for r in reports for h in r.hypotheses), key=lambda h: h.confidence, reverse=True)
    return RCAReport(incident_id=ticket_id, summary=best.summary, hypotheses=hypotheses[:5])


async def investigate_parallel(
    llm: LLMClient,
    make_registry: RegistryFactory,
    ticket_id: str,
    routes: tuple[str, ...] = tuple(SPECIALISTS),
    max_steps: int = 12,
) -> WorkflowResult:
    """并行：所有专家同时查，合并结果。一位专家失败不影响其他人；全部失败才算整体失败。

    代价要说清楚：成本是单个专家的几倍（每位都有自己完整的对话历史），换来的是覆盖面和更短的总耗时
    （总耗时约等于最慢的那一位，而不是它们的总和）。
    """
    outcomes = await asyncio.gather(
        *(_run_specialist(llm, make_registry, route, ticket_id, max_steps) for route in routes),
        return_exceptions=True,
    )
    results, notes, ran = [], [], []
    for route, outcome in zip(routes, outcomes, strict=True):
        if isinstance(outcome, (AgentError, LLMError)):
            notes.append(f"{route}: failed ({type(outcome).__name__})")  # 预期内的失败：记下来，继续
        elif isinstance(outcome, BaseException):
            raise outcome  # 不是预期内的（比如程序 bug、被取消）：不要悄悄吞掉
        else:
            results.append(outcome)
            ran.append(route)
    reports = [r.report for r in results if r.report is not None]
    if not reports:
        raise AgentError(f"no specialist produced a report ({'; '.join(notes)})")
    return WorkflowResult(
        report=merge_reports(ticket_id, reports),
        routes=ran,
        llm_calls=sum(r.llm_calls for r in results),
        input_tokens=sum(r.input_tokens for r in results),
        output_tokens=sum(r.output_tokens for r in results),
        notes=notes,
    )


# ---------------------------------------------------------------- 串联
async def investigate_chained(
    llm: LLMClient,
    make_registry: RegistryFactory,
    ticket_id: str,
    ticket_text: str,
    max_steps: int = 12,
    low_confidence: float = LOW_CONFIDENCE,
) -> WorkflowResult:
    """串联：路由 -> 一位专家 -> 关卡；关卡没通过就升级成全部专家并行。

    每一步的输出决定下一步：分类的标签决定用哪位专家；专家报告里的置信度决定要不要升级。
    好处是常见情况只花一位专家的钱；没把握的情况才多花钱。
    """
    first = await investigate_routed(llm, make_registry, ticket_id, ticket_text, max_steps)
    if first.report.hypotheses[0].confidence >= low_confidence:
        return first
    wider = await investigate_parallel(llm, make_registry, ticket_id, max_steps=max_steps)
    return WorkflowResult(
        report=merge_reports(ticket_id, [first.report, wider.report]),
        routes=first.routes + wider.routes,
        escalated=True,
        llm_calls=first.llm_calls + wider.llm_calls,
        input_tokens=first.input_tokens + wider.input_tokens,
        output_tokens=first.output_tokens + wider.output_tokens,
        notes=[f"escalated: top confidence {first.report.hypotheses[0].confidence:.2f} < {low_confidence}", *wider.notes],
    )
