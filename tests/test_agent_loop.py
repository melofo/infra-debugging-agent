"""手写 agent loop 的测试。

测试 agent 的关键技巧：用"剧本化的假 LLM"（ScriptedLLM）。
我们预先写好模型每一轮会说什么（"先调这个工具，再调那个，最后提交报告"），
这样循环的逻辑（执行工具、回传结果、错误处理、步数上限）可以被确定地、免费地测试。
真正的模型好不好是另一个问题，那是 Phase 7 评估（evals）要回答的。
"""

import pytest

from app.agent.common import AgentError
from app.agent.loop import InvestigationAgent
from app.agent.tools import build_investigation_tools
from app.scenario import load_scenario
from tests.fakes import SUBMIT_TOOL_NAME, VALID_REPORT_ARGS, ScriptedLLM, ToolCall, calls, final_text


@pytest.fixture
def registry():
    return build_investigation_tools(load_scenario("bug-1042"))


async def test_happy_path_runs_tools_then_submits_report(registry):
    llm = ScriptedLLM(
        [
            calls(ToolCall("get_ticket", {"ticket_id": "BUG-1042"})),
            calls(ToolCall("search_logs", {"service": "checkout-api", "level": "ERROR"})),
            calls(ToolCall("get_recent_deploys", {"service": "checkout-api"})),
            calls(ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)),
            final_text(),
        ]
    )
    result = await InvestigationAgent(llm, registry).investigate("BUG-1042")

    assert result.report.incident_id == "BUG-1042"
    assert "maximumPoolSize" in result.report.hypotheses[0].root_cause
    assert [s.tool for s in result.steps] == ["get_ticket", "search_logs", "get_recent_deploys", SUBMIT_TOOL_NAME]
    assert all(s.ok for s in result.steps)
    # token 用量被累加：5 次调用（4 次调查 + 1 次文字收尾），每次 10 + 5。
    assert result.llm_calls == 5
    assert (result.input_tokens, result.output_tokens) == (50, 25)


async def test_tool_results_are_fed_back_to_the_model(registry):
    llm = ScriptedLLM(
        [
            calls(ToolCall("get_ticket", {"ticket_id": "BUG-1042"})),
            calls(ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)),
            final_text(),
        ]
    )
    await InvestigationAgent(llm, registry).investigate("BUG-1042")

    # 第二次调用模型时，历史里必须包含：用户问题、模型的工具请求、工具的执行结果。
    second_call_history = llm.seen_messages[1]
    assert [m.role for m in second_call_history] == ["user", "model", "tool"]
    assert second_call_history[2].tool_results[0].result["service"] == "checkout-api"


async def test_tool_error_is_returned_to_model_not_raised(registry):
    llm = ScriptedLLM(
        [
            calls(ToolCall("delete_database", {})),  # 模型编造了一个不存在的工具
            calls(ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)),
            final_text(),
        ]
    )
    result = await InvestigationAgent(llm, registry).investigate("BUG-1042")

    assert result.steps[0].ok is False
    fed_back = llm.seen_messages[1][-1].tool_results[0]
    assert fed_back.is_error and "unknown tool" in fed_back.result
    assert result.report is not None  # 调查没有因此崩溃


async def test_report_without_evidence_is_rejected_and_model_can_retry(registry):
    bad_args = {
        "summary": "s",
        "hypotheses": [{"root_cause": "guess", "confidence": 0.5, "evidence": []}],
    }
    llm = ScriptedLLM(
        [
            calls(ToolCall(SUBMIT_TOOL_NAME, bad_args)),
            calls(ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)),
            final_text(),
        ]
    )
    result = await InvestigationAgent(llm, registry).investigate("BUG-1042")

    # "没有证据不下结论"由代码强制：第一次提交被拒绝，第二次才通过。
    assert [s.ok for s in result.steps] == [False, True]
    assert "invalid report" in llm.seen_messages[1][-1].tool_results[0].result


async def test_text_only_answer_is_nudged_to_submit(registry):
    llm = ScriptedLLM(
        [
            calls(text="I think it is the database."),  # 没调工具，也没提交
            calls(ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)),
            final_text(),
        ]
    )
    result = await InvestigationAgent(llm, registry).investigate("BUG-1042")

    assert result.report is not None
    assert "submit_rca_report" in llm.seen_messages[1][-1].text


async def test_max_steps_stops_endless_loops(registry):
    # 模型一直在查工单，永不收尾：必须被步数上限截断，而不是无限循环烧钱。
    endless = [calls(ToolCall("get_ticket", {"ticket_id": "BUG-1042"})) for _ in range(3)]
    llm = ScriptedLLM(endless)
    with pytest.raises(AgentError):
        await InvestigationAgent(llm, registry, max_steps=3).investigate("BUG-1042")
    assert len(llm.seen_messages) == 3
