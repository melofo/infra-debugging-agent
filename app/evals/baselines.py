"""不用大模型的"替身"：和真实模型走完全一样的 agent 循环、工具和证据检查，只是回答由固定规则给出。

用途：给评估数字一个参照。准确率 70% 是好是坏？要看"什么都不懂的简单规则"能拿多少分：
  oracle         读标准答案直接交卷。必须拿满分：它证明评分器和数据集没有自相矛盾（上限）。
  latest_deploy  "怪最近一次发布"。一半的场景根本不是发布引起的，所以应该很低：它衡量题目是不是太容易。
  runbook_copy   把最相关的 runbook 原文当作根因。应该很低：它衡量"光靠知识库文档能不能蒙对"。
  ticket_echo    把工单标题当作根因。应该是 0。
真实模型的成绩要和这几条线比，而不是孤立地看。

它们实现 LLMClient 接口（generate_with_tools），所以可以直接放进 InvestigationAgent。
和 tests/fakes.py 里的 AutoLLM 一样是"无状态"的：每次只看对话历史的最后一条决定下一步，并发安全。
"""

from app.agent.common import SUBMIT_TOOL_NAME
from app.llm.base import LLMResponse, Message, ToolCall
from app.scenario import Scenario


def _response(*tool_calls: ToolCall, text: str = "") -> LLMResponse:
    return LLMResponse(text=text, input_tokens=0, output_tokens=0, latency_ms=0, tool_calls=tuple(tool_calls))


def _tool_outputs(messages: list[Message]) -> dict[str, object]:
    """到目前为止，每个工具最近一次成功的返回。"""
    outputs: dict[str, object] = {}
    for message in messages:
        if message.role == "tool":
            for result in message.tool_results:
                if not result.is_error:
                    outputs[result.name] = result.result
    return outputs


def _report(root_cause: str, evidence: list[dict]) -> dict:
    return {
        "summary": root_cause,
        "hypotheses": [
            {"root_cause": root_cause, "confidence": 0.6, "evidence": evidence, "suggested_next_steps": []}
        ],
    }


class ScriptedBaseline:
    """所有替身的共同骨架：先查工单，再按各自的方式取数据，最后提交报告，收尾一句话。"""

    name = "baseline"

    def __init__(self, scenario: Scenario):
        self.scenario = scenario
        self.service = scenario.ticket.service

    async def generate(self, prompt, system=None):
        raise NotImplementedError

    async def generate_with_tools(self, messages, tools, system=None) -> LLMResponse:
        last = messages[-1]
        if last.role == "user" and "Finish by calling" not in last.text:
            return _response(ToolCall("get_ticket", {"ticket_id": self.scenario.ticket.id}))
        if last.role == "tool" and last.tool_results[0].name == SUBMIT_TOOL_NAME:
            return _response(text="Done.")
        outputs = _tool_outputs(messages)
        made = [(c.name, c.args) for m in messages if m.role == "model" for c in m.tool_calls]  # 已经发出过的调用
        next_call = self.next_call(outputs, made)
        if next_call is not None:
            return _response(next_call)
        return _response(ToolCall(SUBMIT_TOOL_NAME, self.final_report(outputs)))

    def next_call(self, outputs: dict, made: list) -> ToolCall | None:
        return None

    def final_report(self, outputs: dict) -> dict:
        raise NotImplementedError


class TicketEcho(ScriptedBaseline):
    name = "ticket_echo"

    def final_report(self, outputs):
        ticket = outputs["get_ticket"]
        return _report(f"{ticket['title']}. {ticket['description']}", [
            {"source": "jira", "summary": "the ticket", "reference": ticket["id"]}
        ])


class LatestDeploy(ScriptedBaseline):
    name = "latest_deploy"

    def next_call(self, outputs, made):
        if "get_recent_deploys" not in outputs:
            return ToolCall("get_recent_deploys", {"service": self.service, "limit": 1})
        return None

    def final_report(self, outputs):
        latest = outputs["get_recent_deploys"][0]  # 工具返回的是按时间倒序，第一个就是最近的
        cause = f"The latest deploy {latest['version']} ({latest['summary']}: {latest['diff_summary']}) broke the service"
        return _report(cause, [{"source": "git", "summary": latest["summary"], "reference": latest["commit"]}])


class RunbookCopy(ScriptedBaseline):
    name = "runbook_copy"

    def next_call(self, outputs, made):
        if "search_runbooks" not in outputs:
            return ToolCall("search_runbooks", {"query": self.scenario.ticket.title})
        return None

    def final_report(self, outputs):
        hits = outputs["search_runbooks"]
        if not hits:  # 关键词检索没命中任何 runbook：没有东西可抄，只能复述工单
            ticket = outputs["get_ticket"]
            return _report(ticket["title"], [{"source": "jira", "summary": "the ticket", "reference": ticket["id"]}])
        top = hits[0]
        return _report(f"{top['title']}: {top['content']}", [
            {"source": "runbook", "summary": top["title"], "reference": top["id"]}
        ])


class Oracle(ScriptedBaseline):
    """读标准答案的"考生"。为了通过证据检查，它也老老实实先调用工具、引用工具真正返回的出处。"""

    name = "oracle"

    def _log_service(self, reference: str) -> str | None:
        return next((l.service for l in self.scenario.logs if reference in l.message), None)

    def next_call(self, outputs, made):
        truth = self.scenario.ground_truth
        if "get_recent_deploys" not in outputs:
            return ToolCall("get_recent_deploys", {"service": self.service})
        # 关键出处可能分散在不同服务的日志里（比如密钥轮换记录在 secrets-manager，报错在业务服务）：每个服务都要查。
        queried = {args["service"] for name, args in made if name == "search_logs"}
        for ref in truth.culprit_references:
            service = self._log_service(ref)
            if service and service not in queried:
                return ToolCall("search_logs", {"service": service, "limit": 50})
        return None

    def final_report(self, outputs):
        truth = self.scenario.ground_truth
        commits = {d["commit"] for d in outputs.get("get_recent_deploys", [])}
        evidence = []
        for ref in truth.culprit_references:
            if ref in commits:
                evidence.append({"source": "git", "summary": "culprit deploy", "reference": ref})
            elif self._log_service(ref):
                evidence.append({"source": "log", "summary": "key log line", "reference": ref if len(ref) >= 20 else self._full_message(ref)})
        if not evidence:
            evidence.append({"source": "jira", "summary": "the ticket", "reference": self.scenario.ticket.id})
        return _report(truth.root_cause, evidence)

    def _full_message(self, reference: str) -> str:
        return next(l.message for l in self.scenario.logs if reference in l.message)


BASELINES: dict[str, type[ScriptedBaseline]] = {
    cls.name: cls for cls in (Oracle, LatestDeploy, RunbookCopy, TicketEcho)
}
