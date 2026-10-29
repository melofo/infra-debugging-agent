"""核心数据结构的测试：确认"不合法的数据造不出对象"。"""

import json
import logging

import pytest
from pydantic import ValidationError

from app.logging_config import JsonFormatter
from app.schemas import Evidence, EvidenceSource, Hypothesis, Incident, RCAReport


def make_evidence() -> Evidence:
    return Evidence(
        source=EvidenceSource.LOG,
        summary="HTTP 500 spike after 14:02",
        reference="logs: service=checkout-api status=500",
    )


def make_hypothesis(cause: str, confidence: float) -> Hypothesis:
    return Hypothesis(root_cause=cause, confidence=confidence, evidence=[make_evidence()])


def test_runbook_evidence_never_has_an_observation_time():
    # Runbook 是静态文档，模型填的 observed_at 会被悄悄清掉；其他来源的时间保留。
    from datetime import datetime, timezone

    moment = datetime(2026, 10, 20, 14, 2, tzinfo=timezone.utc)
    runbook = Evidence(source=EvidenceSource.RUNBOOK, summary="s", reference="RB-101", observed_at=moment)
    log = Evidence(source=EvidenceSource.LOG, summary="s", reference="line", observed_at=moment)
    assert runbook.observed_at is None
    assert log.observed_at == moment


def test_incident_defaults():
    incident = Incident(id="BUG-1042", title="Checkout 500s", service="checkout-api")
    assert incident.severity == "medium"
    # 创建时间默认带时区，避免 naive datetime 的坑。
    assert incident.reported_at.tzinfo is not None


def test_hypothesis_requires_evidence():
    # "没有证据不下结论"：evidence 为空会直接校验失败。
    with pytest.raises(ValidationError):
        Hypothesis(root_cause="DB pool exhausted", confidence=0.8, evidence=[])


def test_hypothesis_confidence_must_be_between_0_and_1():
    with pytest.raises(ValidationError):
        Hypothesis(root_cause="x", confidence=1.5, evidence=[make_evidence()])


def test_report_ranks_hypotheses_by_confidence():
    report = RCAReport(
        incident_id="BUG-1042",
        summary="Likely a connection pool issue",
        hypotheses=[make_hypothesis("bad deploy", 0.2), make_hypothesis("pool exhausted", 0.9)],
    )
    assert report.hypotheses[0].root_cause == "pool exhausted"


def test_report_round_trips_through_json():
    # 序列化成 JSON 再解析回来应当完全一致：这是存数据库、前后端传输、评估对比的基础。
    report = RCAReport(
        incident_id="BUG-1042",
        summary="s",
        hypotheses=[make_hypothesis("pool exhausted", 0.9)],
    )
    assert RCAReport.model_validate_json(report.model_dump_json()) == report


def test_json_formatter_includes_extra_fields():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "llm call", None, None)
    record.latency_ms = 120  # 模拟 logger.info(..., extra={"latency_ms": 120})
    output = json.loads(JsonFormatter().format(record))
    assert output["message"] == "llm call"
    assert output["level"] == "INFO"
    assert output["latency_ms"] == 120
