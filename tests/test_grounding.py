"""证据落地检查的测试：报告里的每条证据，出处必须能在工具真实返回的数据里原样找到。

大多数测试用【真实的场景数据和真实的工具】产生输出，再去核对：
检查逻辑的输入是"工具真实返回的样子"，而不是我们手写的假数据，这样才能发现格式上的不匹配。
"""

import pytest

from app.agent.common import SYSTEM_PROMPT, AgentError, SubmitRCAArgs
from app.agent.grounding import GroundingCorpus, rejection_message
from app.agent.loop import MAX_GROUNDING_REJECTIONS, InvestigationAgent
from app.agent.sdk_afc import SdkAfcAgent
from app.agent.tools import build_investigation_tools
from app.llm.base import LLMError, LLMResponse, Message, ToolResult
from app.mcp_client import connect_mcp
from app.mcp_server import build_mcp_server
from app.scenario import load_scenario
from tests.fakes import (
    GIT_EVIDENCE,
    JIRA_EVIDENCE,
    LOG_EVIDENCE,
    RICH_REPORT_ARGS,
    SUBMIT_TOOL_NAME,
    ScriptedLLM,
    ToolCall,
    calls,
    final_text,
    report_args,
)

LOGS = ("search_logs", {"service": "checkout-api", "level": "ERROR"})
DEPLOYS = ("get_recent_deploys", {"service": "checkout-api"})
TICKET = ("get_ticket", {"ticket_id": "BUG-1042"})
RUNBOOKS = ("search_runbooks", {"query": "connection pool timeout"})


@pytest.fixture
def registry():
    return build_investigation_tools(load_scenario("bug-1042"))


async def corpus_after(registry, *tool_calls) -> GroundingCorpus:
    corpus = GroundingCorpus()
    for name, args in tool_calls:
        corpus.add(name, await registry.execute(name, args))
    return corpus


def check(corpus, *evidence):
    return corpus.check(SubmitRCAArgs.model_validate(report_args(*evidence)))


def ev(source, reference, summary="s"):
    return {"source": source, "summary": summary, "reference": reference}


# ============================================================ 通过的情形
async def test_real_references_from_the_matching_tools_are_accepted(registry):
    corpus = await corpus_after(registry, TICKET, LOGS, DEPLOYS, RUNBOOKS)
    assert check(corpus, JIRA_EVIDENCE, GIT_EVIDENCE, LOG_EVIDENCE, ev("runbook", "RB-101")) == []


async def test_matching_ignores_case_and_extra_whitespace_only(registry):
    corpus = await corpus_after(registry, LOGS)
    sloppy = "hikaripool-1  -   CONNECTION is not available,\nrequest timed out after 30000ms"
    assert check(corpus, ev("log", sloppy)) == []


async def test_a_fragment_of_a_long_message_is_enough(registry):
    corpus = await corpus_after(registry, LOGS)
    assert check(corpus, ev("log", "Connection is not available, request timed out")) == []


async def test_a_short_commit_hash_prefix_is_accepted(registry):
    corpus = await corpus_after(registry, DEPLOYS)
    assert check(corpus, ev("git", "7be41d")) == []


# ============================================================ 拒绝：编造
async def test_a_made_up_commit_is_rejected(registry):
    corpus = await corpus_after(registry, DEPLOYS)
    [problem] = check(corpus, ev("git", "deadbeef"))
    assert "not found" in problem.reason


async def test_a_made_up_log_line_is_rejected_even_when_logs_were_searched(registry):
    corpus = await corpus_after(registry, LOGS)
    [problem] = check(corpus, ev("log", "OutOfMemoryError: Java heap space in checkout-api"))
    assert "not found" in problem.reason


async def test_any_change_to_the_text_is_rejected(registry):
    corpus = await corpus_after(registry, DEPLOYS)
    assert check(corpus, ev("git", "commit 7be41d0"))  # 多了一个词：不是原文
    assert check(corpus, ev("git", "7be41d1"))  # 差一个字符


async def test_a_reference_stitched_across_two_fields_is_rejected(registry):
    """"ERROR" 来自 level 字段，后半句来自 message 字段：拼起来的整句，工具从没返回过。"""
    corpus = await corpus_after(registry, LOGS)
    assert check(corpus, ev("log", "ERROR HikariPool-1 - Connection is not available"))


async def test_an_unknown_document_id_is_rejected(registry):
    corpus = await corpus_after(registry, RUNBOOKS)
    assert check(corpus, ev("runbook", "RB-999"))
    assert check(corpus, ev("runbook", "RB-101")) == []


# ============================================================ 拒绝：来源和工具不匹配
async def test_evidence_type_decides_which_tool_it_may_come_from(registry):
    corpus = await corpus_after(registry, TICKET, LOGS, DEPLOYS)
    # 工单号真实存在，但它来自 get_ticket，不能冒充 git 证据
    assert check(corpus, ev("git", "BUG-1042"))
    # 工单标题不能冒充日志证据
    assert check(corpus, ev("log", "Checkout API intermittently returns HTTP 500"))
    # 日志里的真实消息也不能冒充工单证据
    assert check(corpus, ev("jira", "HikariPool-1 - Connection is not available, request timed out after 30000ms"))


async def test_the_reason_says_when_the_required_tool_was_never_called(registry):
    corpus = await corpus_after(registry, TICKET)
    [problem] = check(corpus, GIT_EVIDENCE)
    assert "get_recent_deploys" in problem.reason and "not called" in problem.reason


async def test_metric_and_trace_evidence_can_never_be_verified(registry):
    corpus = await corpus_after(registry, TICKET, LOGS, DEPLOYS, RUNBOOKS)
    for source in ("metric", "trace"):
        [problem] = check(corpus, ev(source, "p99 latency 4200ms at 14:05 UTC"))
        assert "no tool can provide" in problem.reason


# ============================================================ 拒绝：太短、拿不到的数据
async def test_too_short_references_are_rejected_even_if_they_appear_in_the_output(registry):
    corpus = await corpus_after(registry, TICKET, LOGS)
    assert check(corpus, ev("log", "ERROR"))
    assert check(corpus, ev("log", "checkout-api"))  # 在每一行日志里都有，没有核对价值
    assert check(corpus, ev("jira", "BUG"))


async def test_only_successful_tool_results_count():
    failed = ToolResult("get_recent_deploys", "unknown service, tried 7be41d0", is_error=True)
    corpus = GroundingCorpus.from_messages([Message(role="tool", tool_results=(failed,))])
    assert check(corpus, ev("git", "7be41d0"))


async def test_what_the_user_or_the_model_said_does_not_count():
    corpus = GroundingCorpus.from_messages(
        [
            Message(role="user", text="the culprit is commit 7be41d0 and ticket BUG-1042"),
            Message(role="model", text="Agreed: 7be41d0 shrank the pool"),
        ]
    )
    # 逐条检查：不能让"其中一条本来就过不了"掩盖另一条被错误放行
    assert len(check(corpus, ev("git", "7be41d0"))) == 1
    assert len(check(corpus, ev("jira", "BUG-1042"))) == 1


async def test_the_submitted_report_itself_is_not_a_source():
    submitted = ToolResult(SUBMIT_TOOL_NAME, {"reference": "7be41d0"})
    corpus = GroundingCorpus.from_messages([Message(role="tool", tool_results=(submitted,))])
    assert check(corpus, ev("git", "7be41d0"))


async def test_results_from_the_same_batch_count_but_failed_ones_do_not():
    ok = ToolResult("get_recent_deploys", [{"commit": "7be41d0"}])
    failed = ToolResult("get_recent_deploys", "7be41d0 boom", is_error=True)
    assert check(GroundingCorpus.from_messages([], extra=(ok,)), ev("git", "7be41d0")) == []
    assert check(GroundingCorpus.from_messages([], extra=(failed,)), ev("git", "7be41d0"))


# ============================================================ 反馈给模型的内容
async def test_the_rejection_message_pinpoints_the_evidence_and_truncates_long_text(registry):
    corpus = await corpus_after(registry, TICKET)
    long_reference = "x" * 500
    args = SubmitRCAArgs.model_validate(
        {
            "summary": "s",
            "hypotheses": [
                {"root_cause": "a", "confidence": 0.4, "evidence": [JIRA_EVIDENCE]},
                {"root_cause": "b", "confidence": 0.5, "evidence": [JIRA_EVIDENCE, ev("git", long_reference)]},
            ],
        }
    )
    [problem] = corpus.check(args)
    message = rejection_message([problem])
    assert (problem.hypothesis, problem.evidence) == (2, 2)  # 按模型提交时的顺序编号，它才能对得上
    assert "hypothesis 2, evidence 2" in message
    assert long_reference not in message and "..." in message
    assert "verbatim" in message


def test_the_prompt_tells_the_model_the_rule():
    assert "verbatim" in SYSTEM_PROMPT and "rejected" in SYSTEM_PROMPT


# ============================================================ 在 Agent 循环里
async def test_a_fabricated_report_is_sent_back_and_a_corrected_one_is_accepted(registry):
    fabricated = report_args(ev("git", "commit abc1234"), ev("log", "checkout-api ERROR 14:02"))
    llm = ScriptedLLM(
        [
            calls(ToolCall(*TICKET)),
            calls(ToolCall(SUBMIT_TOOL_NAME, fabricated)),
            calls(ToolCall(SUBMIT_TOOL_NAME, report_args(JIRA_EVIDENCE))),
            final_text(),
        ]
    )
    result = await InvestigationAgent(llm, registry).investigate("BUG-1042")

    assert [s.ok for s in result.steps] == [True, False, True]
    fed_back = llm.seen_messages[2][-1].tool_results[0]
    assert fed_back.is_error and "hypothesis 1, evidence 1" in fed_back.result and "evidence 2" in fed_back.result
    assert result.report is not None


async def test_evidence_gathered_in_the_same_batch_as_the_submission_counts(registry):
    llm = ScriptedLLM([calls(ToolCall(*TICKET), ToolCall(SUBMIT_TOOL_NAME, report_args(JIRA_EVIDENCE))), final_text()])
    result = await InvestigationAgent(llm, registry).investigate("BUG-1042")
    assert [s.ok for s in result.steps] == [True, True] and result.report is not None


async def test_a_submission_placed_before_the_tool_call_in_the_same_batch_is_rejected(registry):
    """调用是按顺序处理的：提交时，排在它后面的工具还没执行，所以它引用不到那份数据。"""
    llm = ScriptedLLM(
        [
            calls(ToolCall(SUBMIT_TOOL_NAME, report_args(JIRA_EVIDENCE)), ToolCall(*TICKET)),
            calls(ToolCall(SUBMIT_TOOL_NAME, report_args(JIRA_EVIDENCE))),  # 这次历史里已有工单结果
            final_text(),
        ]
    )
    result = await InvestigationAgent(llm, registry).investigate("BUG-1042")
    assert [s.ok for s in result.steps] == [False, True, True]


async def test_a_model_that_keeps_fabricating_fails_the_turn_and_leaves_history_clean(registry):
    fabricated = report_args(ev("git", "abc1234"))
    llm = ScriptedLLM([calls(ToolCall(SUBMIT_TOOL_NAME, fabricated)) for _ in range(MAX_GROUNDING_REJECTIONS + 1)])
    messages = []
    with pytest.raises(AgentError, match="no tool returned"):
        await InvestigationAgent(llm, registry).run_turn(messages, "go", "BUG-1042", require_report=True)
    assert messages == []  # 整轮回滚，没有留下半截历史


async def test_the_model_gets_exactly_the_allowed_number_of_retries(registry):
    fabricated = report_args(ev("git", "abc1234"))
    script = [calls(ToolCall(*TICKET))]
    script += [calls(ToolCall(SUBMIT_TOOL_NAME, fabricated)) for _ in range(MAX_GROUNDING_REJECTIONS)]
    script += [calls(ToolCall(SUBMIT_TOOL_NAME, report_args(JIRA_EVIDENCE))), final_text()]
    result = await InvestigationAgent(ScriptedLLM(script), registry).investigate("BUG-1042")
    assert result.report is not None  # 用完全部重试机会后的最后一次改对了，仍然被接受


async def test_a_follow_up_revision_may_cite_data_gathered_in_an_earlier_turn(registry):
    llm = ScriptedLLM(
        [
            calls(ToolCall(*DEPLOYS)),
            calls(ToolCall(SUBMIT_TOOL_NAME, report_args(GIT_EVIDENCE))),
            final_text("done"),
            # 第二轮（追问后要求修订）：没有再查任何工具，直接引用第一轮查到的发布记录
            calls(ToolCall(SUBMIT_TOOL_NAME, report_args(GIT_EVIDENCE))),
            final_text("revised"),
        ]
    )
    agent = InvestigationAgent(llm, registry)
    messages = []
    await agent.run_turn(messages, "investigate", "BUG-1042", require_report=True)
    second = await agent.run_turn(messages, "please revise", "BUG-1042", require_report=False)
    assert second.report is not None and second.answer == "revised"


async def test_a_report_cannot_cite_data_from_a_turn_that_was_rolled_back(registry):
    """第一轮失败被回滚后，它查到的数据不再属于对话；第二轮不能引用那些"从没发生过"的数据。"""

    class Dies(ScriptedLLM):
        async def generate_with_tools(self, messages, tools, system=None):
            if len(self.seen_messages) == 1:
                self.seen_messages.append(list(messages))
                raise LLMError("lost")
            return await super().generate_with_tools(messages, tools, system)

    messages = []
    first = Dies([calls(ToolCall(*DEPLOYS))])
    with pytest.raises(LLMError):
        await InvestigationAgent(first, registry).run_turn(messages, "go", "BUG-1042", require_report=True)
    assert messages == []

    submit = calls(ToolCall(SUBMIT_TOOL_NAME, report_args(GIT_EVIDENCE)))
    second = ScriptedLLM([submit] * (MAX_GROUNDING_REJECTIONS + 1))
    with pytest.raises(AgentError):
        await InvestigationAgent(second, registry).run_turn(messages, "go", "BUG-1042", require_report=True)


async def test_grounding_works_the_same_through_mcp(registry):
    llm = ScriptedLLM(
        [
            calls(ToolCall(*TICKET)),
            calls(ToolCall(*LOGS)),
            calls(ToolCall(*DEPLOYS)),
            calls(ToolCall(SUBMIT_TOOL_NAME, RICH_REPORT_ARGS)),
            final_text(),
        ]
    )
    async with connect_mcp(build_mcp_server(registry)) as tools:  # 工具输出经过了 JSON 序列化再解析
        result = await InvestigationAgent(llm, tools).investigate("BUG-1042")
    assert result.report is not None and len(result.report.hypotheses[0].evidence) == 3


# ============================================================ SDK 版本
class FakeAfcGemini:
    def __init__(self, script):
        self._script = script

    async def generate_with_python_tools(self, prompt, functions, system=None, max_remote_calls=8):
        await self._script({f.__name__: f for f in functions})
        return LLMResponse(text="", input_tokens=1, output_tokens=1, latency_ms=1)


async def test_sdk_version_rejects_fabricated_evidence_then_accepts_a_corrected_report(registry):
    seen = {}

    async def script(tools):
        await tools["get_ticket"](ticket_id="BUG-1042")
        seen["rejected"] = await tools["submit_rca_report"](**report_args(ev("git", "abc1234")))
        seen["accepted"] = await tools["submit_rca_report"](**report_args(JIRA_EVIDENCE))

    result = await SdkAfcAgent(FakeAfcGemini(script), registry).investigate("BUG-1042")

    assert "could not be verified" in seen["rejected"]["error"]
    assert seen["accepted"] == {"status": "accepted"}
    assert [s.ok for s in result.steps] == [True, False, True]
    assert result.report.hypotheses[0].evidence[0].reference == "BUG-1042"


async def test_sdk_version_fails_when_only_fabricated_reports_were_submitted(registry):
    async def script(tools):
        await tools["submit_rca_report"](**report_args(ev("git", "abc1234")))

    with pytest.raises(AgentError):
        await SdkAfcAgent(FakeAfcGemini(script), registry).investigate("BUG-1042")
