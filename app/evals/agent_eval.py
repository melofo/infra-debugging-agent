"""端到端评估：把一批故障场景交给 agent，统计它找对根因的比例，以及代价（时间、token、被打回的次数）。

和检索评估（app/rag/evaluation.py）的区别：那里只测"找得到文档吗"，这里测整条链路——
选哪些工具、怎么综合、引用的证据是否可追溯、最后的结论对不对。

【一次评估要回答的问题】
  准确率     Top-1（排第一的假设对不对）、Top-3（前三个里有没有对的）
  分层       按故障类别、按难度：错在哪一类、是不是越难越错
  可追溯     报告有没有引用到关键出处；证据检查打回了多少次（模型有多爱编造）
  校准       模型说"90% 确定"的时候，到底有多少次是对的
  代价       每个案例的调用次数、token、延迟（P50 / P95）、按单价折算的费用
  可靠性     出错（模型服务失败、超时、没能提交报告）的案例按"答错"计，不能悄悄从分母里剔除

【统计上要老实】30 个案例，Top-1 每多对一个就是 3.3 个百分点；模型每次的回答也不完全一样。
所以结果里带上 Wilson 置信区间，并支持重复运行（--repeat），看同一个案例是不是时对时错。
"""

import asyncio
import math
import time
from dataclasses import asdict, dataclass, field

from app.agent.common import SUBMIT_TOOL_NAME, SYSTEM_PROMPT, SYSTEM_PROMPT_V2, AgentError
from app.agent.loop import InvestigationAgent
from app.agent.tools import build_investigation_tools
from app.evals.scoring import calibration_gap, cites_culprit, correct_rank, percentile
from app.llm.base import LLMClient, LLMError
from app.scenario import load_scenario


@dataclass(frozen=True)
class AgentVariant:
    """被评估的 agent 配置。同一套题用不同配置各跑一遍，才能知道一个改动到底有没有用（对照实验）。"""

    prompt: str
    service_discovery: bool


AGENT_VARIANTS = {
    "v1": AgentVariant(SYSTEM_PROMPT, service_discovery=False),  # 目前线上的配置
    "v2": AgentVariant(SYSTEM_PROMPT_V2, service_discovery=True),  # 实验：服务目录 + "不要只看发布"的提示
}


@dataclass
class CaseResult:
    scenario: str
    category: str
    difficulty: str
    has_culprit_deploy: bool
    repeat: int = 0
    variant: str = "v1"  # 用的是哪个 agent 配置、多大的步数上限：结果文件要能说明"这个数字是在什么条件下测的"
    max_steps: int = 8
    status: str = "ok"  # ok | agent_error | llm_error | timeout
    correct_rank: int | None = None
    top1: bool = False
    top3: bool = False
    cites_culprit: bool = False
    top_confidence: float | None = None
    top_cause: str = ""
    tool_calls: int = 0
    llm_calls: int = 0
    grounding_rejections: int = 0  # 报告因为"证据对不上工具返回的数据"被打回的次数
    schema_rejections: int = 0  # 报告因为格式不合格（比如假设没有证据）被打回的次数
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    error: str = ""
    tools_used: list[str] = field(default_factory=list)
    # 排查过程的记录。失败（超时、步数用完）时也保留已经做了的步骤，这样才能分析"它卡在哪"。
    steps_taken: int = 0
    log_services: list[str] = field(default_factory=list)  # search_logs 查过哪些服务的日志
    # 关键日志所在的服务，agent 有没有去查？没有关键日志出处的场景（只有 commit）是 None。
    # 用来验证一个假设："agent 找不到根因，是不是因为没去查那个服务的日志"。
    key_service_queried: bool | None = None
    cross_service: bool = False  # 关键日志是否在【工单所属服务以外】的服务里
    report: dict | None = None  # 完整报告：之后改了评分规则，可以对已有结果重新评分，不用重新花钱调模型


def key_service_info(scenario, log_services: list[str]) -> tuple[bool | None, bool]:
    """(agent 有没有查过"根因出处"所在服务的日志, 这个服务是不是工单所属服务以外的)。

    "根因出处"指标准答案里的【第一条】关键出处（约定：第一条是说明"发生了什么变更/事件"的那条，
    比如元凶 commit、"Secret ... rotated"、"Feature flag ... enabled"）。如果它是个 commit，答案在发布记录里，
    不涉及"去哪个服务查日志"，返回 (None, False)。只看第一条是为了避免把"次要的、顺带的日志"（比如数据库的慢查询记录，
    而元凶其实在发布记录里）算成"必须去查的服务"，那样会把指标弄得很吵。
    """
    refs = scenario.ground_truth.culprit_references
    if not refs or any(d.commit == refs[0] for d in scenario.deploys):
        return None, False
    services = {l.service for l in scenario.logs if refs[0] in l.message}
    if not services:
        return None, False
    return services <= set(log_services), services != {scenario.ticket.service}


def count_rejections(steps) -> tuple[int, int]:
    grounding = sum(1 for s in steps if s.tool == SUBMIT_TOOL_NAME and not s.ok and s.result_preview.startswith("report rejected"))
    schema = sum(1 for s in steps if s.tool == SUBMIT_TOOL_NAME and not s.ok and s.result_preview.startswith("invalid report"))
    return grounding, schema


async def run_case(
    entry: dict, llm: LLMClient, *, repeat: int = 0, max_steps: int = 8, timeout_seconds: float = 180.0, variant: str = "v1"
) -> CaseResult:
    """跑一个场景。任何失败都变成一条"答错"的记录，而不是让整批评估中断，也不是从统计里消失。"""
    scenario = load_scenario(entry["scenario"])
    result = CaseResult(
        scenario=entry["scenario"],
        category=entry["category"],
        difficulty=entry["difficulty"],
        has_culprit_deploy=entry["has_culprit_deploy"],
        repeat=repeat,
        variant=variant,
        max_steps=max_steps,
    )
    config = AGENT_VARIANTS[variant]
    # 评估用场景自带的关键词版 runbook 检索（不依赖数据库和知识库）：每次运行的条件完全一样，结果可复现。
    tools = build_investigation_tools(scenario, None, service_discovery=config.service_discovery)
    agent = InvestigationAgent(llm, tools, max_steps=max_steps, system_prompt=config.prompt)
    steps: list = []

    async def record(step) -> None:  # 每完成一步就记下来：即使整轮后来失败了，这些记录也还在
        steps.append(step)

    started = time.perf_counter()
    try:
        # 和 agent.investigate() 完全等价（同样的提示语、同样要求产出报告），只是多挂了一个记录步骤的回调。
        run = await asyncio.wait_for(
            agent.run_turn([], f"Investigate ticket {scenario.ticket.id}.", scenario.ticket.id, require_report=True, on_step=record),
            timeout_seconds,
        )
    except TimeoutError:
        result.status, result.error = "timeout", f"no result within {timeout_seconds:g}s"
    except LLMError as exc:
        result.status, result.error = "llm_error", str(exc)[:200]
    except AgentError as exc:
        result.status, result.error = "agent_error", str(exc)[:200]
    else:
        report = run.report
        keywords = scenario.ground_truth.keywords
        result.correct_rank = correct_rank(report, keywords, scenario.ground_truth.must_have)
        result.top1 = result.correct_rank == 1
        result.top3 = result.correct_rank is not None and result.correct_rank <= 3
        result.cites_culprit = cites_culprit(report, scenario.ground_truth.culprit_references)
        result.top_confidence = report.hypotheses[0].confidence
        result.top_cause = report.hypotheses[0].root_cause[:300]
        result.tool_calls = sum(1 for s in run.steps if s.tool != SUBMIT_TOOL_NAME)
        result.llm_calls, result.input_tokens, result.output_tokens = run.llm_calls, run.input_tokens, run.output_tokens
        result.grounding_rejections, result.schema_rejections = count_rejections(run.steps)
        result.tools_used = sorted({s.tool for s in run.steps if s.tool != SUBMIT_TOOL_NAME})
        result.report = report.model_dump(mode="json")
    result.latency_ms = int((time.perf_counter() - started) * 1000)
    result.steps_taken = len(steps)
    result.log_services = sorted({s.args.get("service") for s in steps if s.tool == "search_logs" and s.args.get("service")})
    result.key_service_queried, result.cross_service = key_service_info(scenario, result.log_services)
    if result.status != "ok":  # 失败时也记下它用过哪些工具
        result.tools_used = sorted({s.tool for s in steps if s.tool != SUBMIT_TOOL_NAME})
    return result


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """比例的 95% 置信区间。样本小的时候，比普通的"p ± 误差"可靠得多（不会跑出 0..1 之外）。"""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


def _rate(results: list[CaseResult], predicate) -> dict:
    n = len(results)
    k = sum(1 for r in results if predicate(r))
    low, high = wilson_interval(k, n)
    return {"n": n, "count": k, "rate": round(k / n, 4) if n else None, "ci95": [low, high]}


def _group(results: list[CaseResult], key) -> dict:
    groups: dict[str, list[CaseResult]] = {}
    for r in results:
        groups.setdefault(key(r), []).append(r)
    return {
        name: {"n": len(items), "top1": _rate(items, lambda r: r.top1)["rate"], "top3": _rate(items, lambda r: r.top3)["rate"]}
        for name, items in sorted(groups.items())
    }


def summarize(results: list[CaseResult], price_in_per_million: float = 0.0, price_out_per_million: float = 0.0) -> dict:
    ok = [r for r in results if r.status == "ok"]
    latencies = [r.latency_ms for r in results]
    cost = (sum(r.input_tokens for r in results) * price_in_per_million + sum(r.output_tokens for r in results) * price_out_per_million) / 1e6
    by_case: dict[str, list[bool]] = {}
    for r in results:
        by_case.setdefault(r.scenario, []).append(r.top1)
    return {
        "cases": len(results),
        "top1": _rate(results, lambda r: r.top1),  # 分母是全部案例：出错的按答错计
        "top3": _rate(results, lambda r: r.top3),
        "errors": _rate(results, lambda r: r.status != "ok"),
        "error_kinds": {s: sum(1 for r in results if r.status == s) for s in ("llm_error", "agent_error", "timeout") if any(r.status == s for r in results)},
        "cites_culprit": _rate(ok, lambda r: r.cites_culprit),
        "had_grounding_rejection": _rate(ok, lambda r: r.grounding_rejections > 0),
        # 关键日志在别的服务里的场景（比如密钥轮换记在 secrets-manager、功能开关记在 config-service）：agent 有没有去那个服务查？
        "looked_in_the_key_service_when_it_was_another_service": _rate(
            [r for r in results if r.cross_service], lambda r: r.key_service_queried is True
        ),
        "accuracy_when_it_looked_in_the_key_service": _rate(
            [r for r in results if r.cross_service and r.key_service_queried], lambda r: r.top1
        ),
        "accuracy_when_it_did_not": _rate(
            [r for r in results if r.cross_service and not r.key_service_queried], lambda r: r.top1
        ),
        "total_grounding_rejections": sum(r.grounding_rejections for r in results),
        "total_schema_rejections": sum(r.schema_rejections for r in results),
        "calibration_gap": calibration_gap([(r.top_confidence, r.top1) for r in ok if r.top_confidence is not None]),
        "by_category": _group(results, lambda r: r.category),
        "by_difficulty": _group(results, lambda r: r.difficulty),
        "by_culprit_deploy": _group(results, lambda r: "has culprit deploy" if r.has_culprit_deploy else "no culprit deploy"),
        "unstable_cases": sorted(name for name, hits in by_case.items() if len(hits) > 1 and 0 < sum(hits) < len(hits)),
        "cost": {
            "avg_tool_calls": round(sum(r.tool_calls for r in ok) / len(ok), 2) if ok else None,
            "avg_llm_calls": round(sum(r.llm_calls for r in ok) / len(ok), 2) if ok else None,
            "avg_input_tokens": round(sum(r.input_tokens for r in ok) / len(ok)) if ok else None,
            "avg_output_tokens": round(sum(r.output_tokens for r in ok) / len(ok)) if ok else None,
            "latency_ms_p50": percentile(latencies, 0.5),
            "latency_ms_p95": percentile(latencies, 0.95),
            "estimated_cost_usd": round(cost, 4) if (price_in_per_million or price_out_per_million) else None,
        },
    }


def result_to_dict(result: CaseResult) -> dict:
    return asdict(result)


def result_from_dict(data: dict) -> CaseResult:
    return CaseResult(**data)
