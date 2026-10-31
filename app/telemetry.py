"""可观测性：用 OpenTelemetry 记录"一次调查里到底发生了什么、花了多久、花了多少 token"。

【三种信号，各回答不同的问题】
  日志 (logs)     "发生了什么"        一条条离散的事件（已有，JSON 日志）
  追踪 (traces)   "这一次请求/任务经过了哪些步骤，每步多久"   一棵 span 树：job -> turn -> 模型调用 / 工具调用
  指标 (metrics)  "整体趋势怎么样"      聚合的数字：每分钟多少次调查、token 总量、工具调用耗时分布
Java 类比：Micrometer + Zipkin/Jaeger 的组合；OpenTelemetry 是其中与厂商无关的标准，
同一份埋点可以送到 Cloud Trace、Jaeger、Datadog 等任何后端，只需要换导出器的配置。

【几个关键的设计选择】
  1. 业务代码只依赖 opentelemetry-api（很小）。没有配置导出器时，所有埋点都是"空操作"，几乎没有开销，
     也不会因为监控后端挂了而影响业务。SDK 和导出器只在 configure_telemetry 里用到。
  2. 埋点只记录【元数据】：工具名、token 数、步数、结果状态、耗时。绝不记录提示词、日志内容、工单正文、工具返回值：
     追踪系统通常权限更宽、保留更久，里面的内容比应用日志更容易外泄。所有属性都过 clean_attributes：
     只接受数字/布尔/短字符串，其他一律丢弃或截断。
  3. 会话 id 是"知道它就能访问会话"的凭证，不能原样写进追踪：只记它的哈希前缀。
  4. 通过依赖注入（use_providers）拿到 tracer 和 meter，而不是改 OpenTelemetry 的全局状态：
     全局提供者只能设置一次，测试里没法各用各的；注入式的写法两边都方便。
  5. 采样：高流量时不可能保存每一条追踪（贵）。按比例采样（otel_sample_ratio），并且"跟随父级"：
     一棵树要么整棵保留，要么整棵丢弃，不会出现缺了中间一层的残缺追踪。
"""

import hashlib
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.trace import Span, Status, StatusCode

logger = logging.getLogger(__name__)

SERVICE_NAME = "infra-debugging-agent"
MAX_ATTRIBUTE_CHARS = 200


def clean_attributes(attributes: dict[str, Any]) -> dict[str, str | int | float | bool]:
    """只放行数字、布尔和短字符串；None 丢弃；其他类型（列表、字典、对象）转成它的类型名。

    这是防止"顺手把整段日志/提示词塞进追踪"的最后一道关卡：就算调用方写错了，内容也出不去。
    """
    cleaned: dict[str, str | int | float | bool] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, (bool, int, float)):
            cleaned[key] = value
        elif isinstance(value, str):
            cleaned[key] = value if len(value) <= MAX_ATTRIBUTE_CHARS else value[:MAX_ATTRIBUTE_CHARS] + "…"
        else:
            cleaned[key] = f"<{type(value).__name__}>"
    return cleaned


def opaque_id(value: str) -> str:
    """把"凭证性质的 id"（比如会话 id）变成不可逆的短标识，只能用来在追踪里区分，不能拿去访问。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


class _Instruments:
    def __init__(self, tracer: trace.Tracer, meter: metrics.Meter):
        self.tracer = tracer
        self.turns = meter.create_counter("agent.turns", description="Agent turns by outcome")
        self.tokens = meter.create_counter("llm.tokens", description="Model tokens by direction")
        self.llm_duration = meter.create_histogram("llm.call.duration", unit="ms", description="Duration of model calls")
        self.tool_duration = meter.create_histogram("tool.call.duration", unit="ms", description="Duration of tool calls")
        self.rejections = meter.create_counter("agent.report.rejections", description="Reports sent back to the model")
        self.jobs = meter.create_counter("job.outcomes", description="Background job outcomes")


# 默认用全局 API：没有配置 SDK 时是空操作。
_instruments = _Instruments(trace.get_tracer(SERVICE_NAME), metrics.get_meter(SERVICE_NAME))


def use_providers(tracer_provider: Any | None, meter_provider: Any | None) -> None:
    """让埋点改用给定的提供者（传 None 恢复为全局/空操作）。配置和测试都走这里。"""
    global _instruments
    tracer = tracer_provider.get_tracer(SERVICE_NAME) if tracer_provider else trace.get_tracer(SERVICE_NAME)
    meter = meter_provider.get_meter(SERVICE_NAME) if meter_provider else metrics.get_meter(SERVICE_NAME)
    _instruments = _Instruments(tracer, meter)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Span]:
    """开一个 span。with 块里抛出异常时，span 自动记录异常并标记为 ERROR（OpenTelemetry 的默认行为），然后异常继续往外传。"""
    with _instruments.tracer.start_as_current_span(name, attributes=clean_attributes(attributes)) as current:
        yield current


def set_attributes(current: Span, **attributes: Any) -> None:
    for key, value in clean_attributes(attributes).items():
        current.set_attribute(key, value)


def mark_error(current: Span, description: str) -> None:
    """业务上的失败（不是异常）也要让 span 显示为错误，这样在追踪界面里一眼能筛出来。"""
    current.set_status(Status(StatusCode.ERROR, description[:MAX_ATTRIBUTE_CHARS]))


def record_llm_call(current: Span, response: Any, seconds: float) -> None:
    set_attributes(
        current,
        **{
            "llm.input_tokens": response.input_tokens,
            "llm.output_tokens": response.output_tokens,
            "llm.tool_calls": len(response.tool_calls),
        },
    )
    _instruments.tokens.add(response.input_tokens, {"direction": "input"})
    _instruments.tokens.add(response.output_tokens, {"direction": "output"})
    _instruments.llm_duration.record(seconds * 1000)


def record_tool_call(current: Span, tool: str, ok: bool, seconds: float, result_chars: int = 0) -> None:
    set_attributes(current, **{"tool.ok": ok, "tool.result_chars": result_chars})
    if not ok:
        mark_error(current, "tool call failed")
    _instruments.tool_duration.record(seconds * 1000, {"tool": tool, "ok": ok})


def record_rejection(kind: str) -> None:
    """报告被打回（grounding = 证据对不上工具返回的数据；schema = 格式不合格）。同时在当前 span 上留一个事件。"""
    _instruments.rejections.add(1, {"kind": kind})
    trace.get_current_span().add_event("report.rejected", {"kind": kind})


def record_turn(outcome: str) -> None:
    _instruments.turns.add(1, {"outcome": outcome})


def record_job(outcome: str) -> None:
    _instruments.jobs.add(1, {"outcome": outcome})


def current_ids() -> tuple[str, str] | None:
    """当前 span 的 (trace_id, span_id)，十六进制；没有有效的 span 时返回 None。用来让日志和追踪能互相跳转。"""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x"), format(context.span_id, "016x")


class Stopwatch:
    def __init__(self) -> None:
        self._start = time.perf_counter()

    def elapsed(self) -> float:
        return time.perf_counter() - self._start


# ---------------------------------------------------------------------------
# 配置：根据设置创建真实的导出器（只在应用启动时调用一次）
# ---------------------------------------------------------------------------
@dataclass
class TelemetryHandle:
    tracer_provider: Any | None = None
    meter_provider: Any | None = None

    def shutdown(self) -> None:
        """关闭前把缓冲区里还没发出去的数据发完（批量导出器是攒一批再发的，不 flush 会丢最后一批）。"""
        for provider in (self.tracer_provider, self.meter_provider):
            if provider is not None:
                try:
                    provider.shutdown()
                except Exception:
                    logger.warning("telemetry shutdown failed", exc_info=True)
        use_providers(None, None)


def configure_telemetry(exporter: str, endpoint: str, sample_ratio: float, environment: str, stream=None) -> TelemetryHandle:
    """exporter: none = 不导出（默认，空操作）；console = 打印到 stderr（本地看看）；otlp = 通过 OTLP/HTTP 发给收集器。

    OTLP 的认证头等敏感配置用标准环境变量 OTEL_EXPORTER_OTLP_HEADERS 提供，由导出器自己读取，
    不放进我们的配置对象（配置对象会被打印、记日志）。
    """
    if exporter == "none":
        return TelemetryHandle()

    # SDK 只在这里导入：没开监控时，连 SDK 都不会被加载。
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.sdk.trace.sampling import ParentBasedTraceIdRatio

    resource = Resource.create({"service.name": SERVICE_NAME, "deployment.environment": environment})
    out = stream or sys.stderr  # 不能是 stdout：stdio 的 MCP 服务器用 stdout 当协议通道
    if exporter == "console":
        span_exporter = ConsoleSpanExporter(out=out)
        metric_exporter = ConsoleMetricExporter(out=out)
    elif exporter == "otlp":
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        base = endpoint.rstrip("/")
        span_exporter = OTLPSpanExporter(endpoint=f"{base}/v1/traces")
        metric_exporter = OTLPMetricExporter(endpoint=f"{base}/v1/metrics")
    else:
        raise ValueError(f"unknown exporter {exporter!r}")

    tracer_provider = TracerProvider(resource=resource, sampler=ParentBasedTraceIdRatio(sample_ratio))
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    meter_provider = MeterProvider(resource=resource, metric_readers=[PeriodicExportingMetricReader(metric_exporter)])
    use_providers(tracer_provider, meter_provider)
    return TelemetryHandle(tracer_provider, meter_provider)
