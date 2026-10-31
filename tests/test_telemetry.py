"""可观测性（OpenTelemetry）的测试。

用内存导出器直接检查"产生了哪些 span、父子关系、属性、指标"。
重点有三类：
  1. 结构对不对：一轮 = 一个根 span，下面是模型调用和工具调用；失败、取消、被打回都有记录；
  2. 会不会泄露：任何 span / 事件的属性里都不能出现提示词、日志内容、工单正文、会话 id；
  3. 会不会拖累业务：没配置导出器时完全不影响运行；监控是辅助，不能让它变成故障源。
"""

import asyncio
import io
import json
import logging

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app import jobs, telemetry
from app.agent.loop import InvestigationAgent
from app.agent.tools import build_investigation_tools
from app.api.chat import get_llm_client
from app.config import Settings
from app.llm.base import LLMError
from app.logging_config import JsonFormatter
from app.main import app
from app.scenario import load_scenario
from app.sessions import SessionStore
from app.worker import Worker
from tests.fakes import (
    GET_TICKET,
    GIT_EVIDENCE,
    JIRA_EVIDENCE,
    SUBMIT_TOOL_NAME,
    VALID_REPORT_ARGS,
    AutoLLM,
    ScriptedLLM,
    ToolCall,
    calls,
    final_text,
    report_args,
)


@pytest.fixture
def otel():
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    telemetry.use_providers(tracer_provider, MeterProvider(metric_readers=[reader]))
    yield exporter, reader
    telemetry.use_providers(None, None)


def points(reader, name):
    """某个指标的所有数据点：[(属性字典, 值或计数)]。"""
    found = []
    data = reader.get_metrics_data()
    for resource in (data.resource_metrics if data else []):
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    for p in metric.data.data_points:
                        found.append((dict(p.attributes), getattr(p, "value", None) if hasattr(p, "value") else p.count))
    return found


def by_name(exporter, name):
    return [s for s in exporter.get_finished_spans() if s.name == name]


def investigation(llm):
    registry = build_investigation_tools(load_scenario("bug-1042"))
    return InvestigationAgent(llm, registry)


def good_script():
    return ScriptedLLM([calls(ToolCall("get_ticket", {"ticket_id": "BUG-1042"})), calls(ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)), final_text()])


# ============================================================ 结构：一轮 = 一棵 span 树
async def test_one_turn_is_one_root_span_with_a_child_per_model_call_and_tool_call(otel):
    exporter, _ = otel
    result = await investigation(good_script()).investigate("BUG-1042")

    [turn] = by_name(exporter, "agent.turn")
    llm_spans, tool_spans = by_name(exporter, "llm.generate"), by_name(exporter, "tool.execute")
    assert turn.parent is None
    assert len(llm_spans) == result.llm_calls == 3 and [s.attributes["tool.name"] for s in tool_spans] == ["get_ticket"]
    assert all(s.parent.span_id == turn.context.span_id for s in llm_spans + tool_spans)  # 都是根的子节点
    assert all(s.context.trace_id == turn.context.trace_id for s in exporter.get_finished_spans())  # 同一条追踪
    attrs = turn.attributes
    assert (attrs["ticket.id"], attrs["agent.llm_calls"], attrs["agent.input_tokens"], attrs["agent.output_tokens"], attrs["agent.has_report"]) == ("BUG-1042", 3, 30, 15, True)
    assert llm_spans[0].attributes["llm.input_tokens"] == 10 and tool_spans[0].attributes["tool.ok"] is True
    assert turn.status.status_code != StatusCode.ERROR


async def test_metrics_add_up_to_what_the_investigation_reports(otel):
    _, reader = otel
    await investigation(good_script()).investigate("BUG-1042")
    tokens = dict((a["direction"], v) for a, v in points(reader, "llm.tokens"))
    assert tokens == {"input": 30, "output": 15}
    assert points(reader, "agent.turns") == [({"outcome": "ok"}, 1)]
    assert [(a["tool"], a["ok"], c) for a, c in points(reader, "tool.call.duration")] == [("get_ticket", True, 1)]
    assert sum(c for _, c in points(reader, "llm.call.duration")) == 3


async def test_a_failing_tool_marks_its_span_but_not_the_whole_turn(otel):
    exporter, _ = otel
    llm = ScriptedLLM([calls(ToolCall("delete_database", {})), calls(GET_TICKET, ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)), final_text()])
    await investigation(llm).investigate("BUG-1042")
    bad = next(s for s in by_name(exporter, "tool.execute") if s.attributes["tool.name"] == "delete_database")
    assert bad.attributes["tool.ok"] is False and bad.status.status_code == StatusCode.ERROR
    assert by_name(exporter, "agent.turn")[0].status.status_code != StatusCode.ERROR


async def test_rejected_reports_are_recorded_as_events_and_counted_by_kind(otel):
    exporter, reader = otel
    fabricated = report_args({"source": "git", "summary": "s", "reference": "abc1234"})
    invalid = {"summary": "s", "hypotheses": [{"root_cause": "x", "confidence": 0.5, "evidence": []}]}
    llm = ScriptedLLM([calls(GET_TICKET, ToolCall(SUBMIT_TOOL_NAME, fabricated)), calls(ToolCall(SUBMIT_TOOL_NAME, invalid)), calls(ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)), final_text()])
    await investigation(llm).investigate("BUG-1042")

    events = [e.attributes["kind"] for e in by_name(exporter, "agent.turn")[0].events if e.name == "report.rejected"]
    assert events == ["grounding", "schema"]
    assert sorted((a["kind"], v) for a, v in points(reader, "agent.report.rejections")) == [("grounding", 1), ("schema", 1)]


async def test_a_model_failure_marks_both_spans_as_errors_and_is_counted(otel):
    exporter, reader = otel

    class Broken(ScriptedLLM):
        async def generate_with_tools(self, messages, tools, system=None):
            raise LLMError("quota exhausted")

    with pytest.raises(LLMError):
        await investigation(Broken([])).investigate("BUG-1042")
    for name in ("agent.turn", "llm.generate"):
        [s] = by_name(exporter, name)
        assert s.status.status_code == StatusCode.ERROR and any(e.name == "exception" for e in s.events), name
    assert points(reader, "agent.turns") == [({"outcome": "error"}, 1)]


async def test_a_cancelled_turn_is_counted_as_cancelled_not_as_an_error(otel):
    _, reader = otel

    class Hangs(ScriptedLLM):
        async def generate_with_tools(self, messages, tools, system=None):
            await asyncio.sleep(3600)

    task = asyncio.create_task(investigation(Hangs([])).investigate("BUG-1042"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert points(reader, "agent.turns") == [({"outcome": "cancelled"}, 1)]


# ============================================================ 不泄露内容
def all_text(exporter) -> str:
    """所有 span 的名字、属性、事件属性拼成一个字符串（用来检查"某段内容绝不能出现"）。"""
    parts = []
    for s in exporter.get_finished_spans():
        parts.append(s.name)
        parts.extend(f"{k}={v}" for k, v in (s.attributes or {}).items())
        for e in s.events:
            parts.extend(f"{k}={v}" for k, v in (e.attributes or {}).items())
    return "\n".join(parts)


async def test_no_prompt_log_ticket_or_report_content_reaches_the_trace(otel):
    exporter, _ = otel
    scenario = load_scenario("bug-1042")
    # 工具参数里可能带着模型自己选的搜索词（来自日志、工单的内容），同样不能进追踪
    llm = ScriptedLLM([calls(ToolCall("search_logs", {"service": "checkout-api", "query": "DISTINCTIVE-SEARCH-TEXT-42"})), calls(GET_TICKET, ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)), final_text("a long free-form answer")])
    await investigation(llm).investigate("BUG-1042")

    text = all_text(exporter)
    secrets = [scenario.ticket.description, scenario.ticket.title, "Investigate ticket", "a long free-form answer", VALID_REPORT_ARGS["summary"], "DISTINCTIVE-SEARCH-TEXT-42"]
    secrets += [entry.message for entry in scenario.logs]
    assert [s for s in secrets if s in text] == []


def test_attributes_are_sanitised_whatever_the_caller_passes():
    cleaned = telemetry.clean_attributes({"a": 1, "b": 2.5, "c": True, "d": None, "e": "x" * 500, "f": ["secret", "list"], "g": {"k": "v"}, "h": object()})
    assert cleaned["a"] == 1 and cleaned["b"] == 2.5 and cleaned["c"] is True and "d" not in cleaned
    assert len(cleaned["e"]) == telemetry.MAX_ATTRIBUTE_CHARS + 1 and cleaned["e"].endswith("…")
    assert cleaned["f"] == "<list>" and cleaned["g"] == "<dict>" and cleaned["h"] == "<object>"  # 内容出不去，只留类型名


def test_opaque_ids_are_stable_short_and_do_not_contain_the_original():
    secret = "super-secret-session-id-123"
    first = telemetry.opaque_id(secret)
    assert first == telemetry.opaque_id(secret) and len(first) == 10 and first != telemetry.opaque_id(secret + "x")
    assert secret not in first


# ============================================================ 不拖累业务
async def test_everything_works_without_any_exporter_configured():
    telemetry.use_providers(None, None)  # 默认：空操作
    result = await investigation(good_script()).investigate("BUG-1042")
    assert result.report is not None and telemetry.current_ids() is None  # 没有有效的 span，也不影响运行


# ============================================================ 后台任务和会话
async def test_a_job_is_one_root_span_with_its_attempt_and_outcome(otel, pool):
    exporter, reader = otel
    await jobs.enqueue(pool, "evt-otel-1", "BUG-1042", "bug-1042")
    await jobs.enqueue(pool, "evt-otel-2", "BUG-9999", "does-not-exist")
    worker = Worker(pool, AutoLLM(), Settings(_env_file=None), worker_id="w1")
    await worker.run_once()
    await worker.run_once()

    spans = {s.attributes["ticket.id"]: s for s in by_name(exporter, "job.process")}
    ok, bad = spans["BUG-1042"], spans["BUG-9999"]
    assert ok.attributes["job.outcome"] == "succeeded" and ok.attributes["job.attempt"] == 1 and ok.status.status_code != StatusCode.ERROR
    assert bad.attributes["job.outcome"] == "invalid_job" and bad.status.status_code == StatusCode.ERROR
    turn = by_name(exporter, "agent.turn")[0]
    assert turn.parent.span_id == ok.context.span_id  # 调查是任务的子树：能看出"这个任务里发生了什么"
    assert sorted(a["outcome"] for a, _ in points(reader, "job.outcomes")) == ["invalid_job", "succeeded"]


async def test_a_session_turn_is_traced_without_the_session_id(otel):
    exporter, _ = otel
    store = SessionStore()
    app.state.session_store = store
    app.dependency_overrides[get_llm_client] = lambda: good_script()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            session_id = (await client.post("/sessions", json={})).json()["id"]
            assert (await client.post(f"/sessions/{session_id}/messages", json={"message": "go"})).status_code == 200
    finally:
        app.dependency_overrides.clear()
    [session_span] = by_name(exporter, "session.turn")
    assert session_span.attributes["session.id_hash"] == telemetry.opaque_id(session_id) and session_span.attributes["session.turn_index"] == 1
    assert session_id not in all_text(exporter)  # 凭证性质的 id 绝不进追踪
    assert by_name(exporter, "agent.turn")[0].parent.span_id == session_span.context.span_id


# ============================================================ 日志和追踪互相关联
def format_log(message="hello") -> dict:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, message, None, None)
    return json.loads(JsonFormatter().format(record))


def test_logs_inside_a_span_carry_the_trace_and_span_ids_and_logs_outside_do_not(otel):
    assert "trace_id" not in format_log()
    with telemetry.span("work") as current:
        line = format_log()
        context = current.get_span_context()
        assert line["trace_id"] == format(context.trace_id, "032x") and line["span_id"] == format(context.span_id, "016x")
        assert len(line["trace_id"]) == 32 and len(line["span_id"]) == 16
    assert "trace_id" not in format_log()


# ============================================================ 配置：导出器和采样
def test_by_default_nothing_is_exported_and_no_sdk_provider_is_created():
    handle = telemetry.configure_telemetry("none", "http://x", 1.0, "test")
    assert handle.tracer_provider is None and handle.meter_provider is None
    handle.shutdown()
    assert Settings(_env_file=None).otel_exporter == "none"


def test_the_console_exporter_writes_spans_to_the_given_stream_and_flushes_on_shutdown():
    stream = io.StringIO()
    handle = telemetry.configure_telemetry("console", "http://x", 1.0, "test", stream=stream)
    with telemetry.span("visible.work", **{"x": 1}):
        pass
    handle.shutdown()  # 批量导出器攒着一批，shutdown 时才一起写出去
    assert "visible.work" in stream.getvalue() and "infra-debugging-agent" in stream.getvalue()
    assert telemetry.current_ids() is None


def test_sampling_ratio_zero_drops_whole_trees_and_one_keeps_them():
    dropped, kept = io.StringIO(), io.StringIO()
    handle = telemetry.configure_telemetry("console", "http://x", 0.0, "test", stream=dropped)
    with telemetry.span("root"):
        with telemetry.span("child"):
            pass
    handle.shutdown()
    handle = telemetry.configure_telemetry("console", "http://x", 1.0, "test", stream=kept)
    with telemetry.span("root"):
        with telemetry.span("child"):
            pass
    handle.shutdown()
    assert "root" not in dropped.getvalue() and "child" not in dropped.getvalue()  # 父级没被采样，整棵都丢（不会有残缺的树）
    assert '"root"' in kept.getvalue() and '"child"' in kept.getvalue()


def test_the_otlp_exporter_can_be_built_without_a_collector_running():
    handle = telemetry.configure_telemetry("otlp", "http://localhost:4318/", 1.0, "test")
    assert handle.tracer_provider is not None and handle.meter_provider is not None
    handle.shutdown()


def test_unknown_exporters_and_out_of_range_sampling_are_rejected():
    with pytest.raises(ValueError):
        telemetry.configure_telemetry("carrier-pigeon", "http://x", 1.0, "test")
    with pytest.raises(Exception, match="OTEL_SAMPLE_RATIO"):
        Settings(_env_file=None, otel_sample_ratio=1.5)
    with pytest.raises(Exception):
        Settings(_env_file=None, otel_exporter="nonsense")


def test_the_application_flushes_telemetry_when_it_shuts_down(monkeypatch):
    from fastapi.testclient import TestClient

    class Spy:
        stopped = False

        def shutdown(self):
            Spy.stopped = True

    seen = {}

    def fake_configure(exporter, endpoint, ratio, environment):
        seen.update(exporter=exporter, ratio=ratio)
        return Spy()

    monkeypatch.setattr("app.main.configure_telemetry", fake_configure)
    with TestClient(app):
        assert Spy.stopped is False  # 运行期间不关闭
    assert Spy.stopped is True and seen == {"exporter": "none", "ratio": 1.0}


def test_console_output_defaults_to_stderr_never_stdout(capsys):
    """stdio 方式的 MCP 服务器用 stdout 当协议通道：往 stdout 多写一个字节，对方就解析失败。"""
    handle = telemetry.configure_telemetry("console", "http://x", 1.0, "test")
    with telemetry.span("stderr.only"):
        pass
    handle.shutdown()
    captured = capsys.readouterr()
    assert captured.out == "" and "stderr.only" in captured.err


def test_the_trace_viewer_script_prints_the_tree_without_any_content(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "show_trace.py"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "GEMINI_API_KEY", "GEMINI_MODEL")}
    result = subprocess.run([sys.executable, str(script), "--scenario", "bug-2010"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0].startswith("agent.turn") and any(l.startswith("  llm.generate") for l in lines) and any(l.startswith("  tool.execute") for l in lines)
    scenario = load_scenario("bug-2010")
    assert scenario.ticket.description not in result.stdout and all(e.message not in result.stdout for e in scenario.logs)
    refused = subprocess.run([sys.executable, str(script), "--llm", "gemini"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert refused.returncode == 2 and "Traceback" not in refused.stderr
    assert subprocess.run([sys.executable, str(script), "--scenario", "nope"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60).returncode == 2
