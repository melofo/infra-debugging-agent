"""核心数据结构（data contract）：故障、证据、根因假设、RCA 报告。

整个项目围着这几个类转：Agent 的输出、Chat UI 的展示、评估打分都依赖它们。
先把数据结构定义清楚，后面各模块才不会各说各话。

Java 类比：Pydantic 的 BaseModel ≈ DTO / POJO + Bean Validation（@NotNull、@Size、@Min...）。
区别是：Pydantic 在创建对象时【自动校验并抛异常】，不合法的数据根本造不出对象。
它还能自动转成/解析 JSON，FastAPI 就是靠它生成 OpenAPI 文档和做请求校验的。

RCA = Root Cause Analysis，根因分析。
"""

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, model_validator


def _utcnow() -> datetime:
    # 一律用带时区的 UTC 时间。Java 里对应 Instant / OffsetDateTime；
    # 不带时区的 datetime（naive）是 Python 里常见的 bug 来源。
    return datetime.now(timezone.utc)


class Severity(str, Enum):
    # 同时继承 str 和 Enum：序列化成 JSON 时就是 "high" 这样的字符串，而不是对象。
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EvidenceSource(str, Enum):
    """证据来自哪类数据源，对应我们之后要写的 Connector。"""

    JIRA = "jira"
    LOG = "log"
    METRIC = "metric"
    TRACE = "trace"
    GIT = "git"
    RUNBOOK = "runbook"
    INCIDENT = "incident"  # 历史事故复盘（知识库里的 INC-xxxx 文档）


class Incident(BaseModel):
    """一次待调查的故障，例如 Jira 里的 BUG-1042。"""

    id: str = Field(min_length=1, examples=["BUG-1042"])
    title: str = Field(min_length=1)
    description: str = ""
    service: str = Field(min_length=1, description="出问题的服务名，如 checkout-api")
    severity: Severity = Severity.MEDIUM
    reported_at: datetime = Field(default_factory=_utcnow)


class Evidence(BaseModel):
    """一条证据：Agent 调用某个工具后拿到的、能支持或反驳某个假设的事实。

    必须记录 source 和 reference（例如日志查询语句、commit SHA、runbook 链接），
    这样工程师才能点回去自己验证，而不是只能相信 LLM 的转述。
    """

    source: EvidenceSource
    summary: str = Field(min_length=1, description="这条证据说明了什么")
    reference: str = Field(min_length=1, description="可追溯的出处，如 commit SHA 或查询语句")
    observed_at: datetime | None = None

    @model_validator(mode="after")
    def _runbook_has_no_observation_time(self) -> "Evidence":
        # Runbook 是静态文档，没有"发生时间"。模型偶尔会顺手填上别的时间（比如工单时间），
        # 这是结构上不合理的数据。这里选择【悄悄纠正】而不是拒绝：
        # 拒绝会让模型多重试一整圈（多一次调用、多花钱），而这个错误无害，纠正就够了。
        # 对比"假设必须有证据"：那条是实质性缺陷，所以选择拒绝并让模型重写。两种处理要分清。
        if self.source == EvidenceSource.RUNBOOK:
            self.observed_at = None
        return self


class Hypothesis(BaseModel):
    """一个根因假设。

    关键约束：evidence 至少一条（min_length=1）。
    这把 "没有证据不下结论" 变成了【代码级别的强制规则】，不靠 prompt 里叮嘱模型。
    这就是之前说的 "安全规则由后端强制，不依赖 LLM 自觉" 的一个小例子。
    """

    root_cause: str = Field(min_length=1)
    # ge/le = greater-or-equal / less-or-equal，相当于 Bean Validation 的 @DecimalMin/@DecimalMax。
    confidence: float = Field(ge=0.0, le=1.0, description="0 到 1 之间的置信度")
    evidence: list[Evidence] = Field(min_length=1)
    suggested_next_steps: list[str] = Field(default_factory=list)


class RCAReport(BaseModel):
    """Agent 的最终输出：一份根因分析报告。"""

    incident_id: str
    summary: str = Field(min_length=1)
    hypotheses: list[Hypothesis] = Field(min_length=1)
    created_at: datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def _rank_hypotheses(self) -> "RCAReport":
        # 模型 "after" 校验器：所有字段通过各自校验后才执行，可以做跨字段/整体处理。
        # 这里把假设按置信度从高到低排好，让 hypotheses[0] 永远是最可能的根因。
        # 后面做评估时 Top-1 / Top-3 准确率就是看这个排序的前 1 / 前 3 个。
        self.hypotheses.sort(key=lambda h: h.confidence, reverse=True)
        return self
