"""生成端到端评估用的故障场景（30 个开发场景 + 6 个留出场景，外加手写的 BUG-1042）。

【为什么要生成，而不是手写 30 个 JSON】
手写会很快变得前后不一致（时间对不上、字段漏填），而且改一个设计就要改 30 个文件。
这里用"原型（archetype）+ 变体"：10 种根因，每种 3 个变体（简单 / 中等 / 困难）。生成器是确定性的
（没有随机数、没有当前时间），所以输出可以提交进仓库，并且有测试保证"提交的文件 == 生成器的输出"。

【场景要能真正区分好坏 agent，而不是人人满分】
  - 一半的原型【没有】元凶发布：根因是下游依赖、证书过期、密钥轮换、流量激增、功能开关……
    只会"怪最近一次发布"的 agent 会在这里全错；
  - 困难变体里，故障前不久有一次【无辜的发布】（改个文案、升级日志库），是故意放的干扰项；
  - 每个场景都有无关的噪声日志（正常的慢请求、缓存未命中），不是只有一条日志写着答案；
  - 工单标题和描述只写【症状】，不泄露原因（有测试检查标准答案的关键词不会出现在工单里）；
  - 相关的 runbook 列出好几种可能的原因，要靠日志才能判断是哪一种。

标准答案（ground_truth）和证据放在同一个文件里，但工具读不到它（见 app/scenario.py 的说明）。
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

FIRST_BUG_NUMBER = 2001
BASE_DAY = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)


@dataclass
class Parts:
    """一个场景里"和根因有关"的部分；噪声、干扰项、runbook 由通用代码补上。"""

    title: str
    description: str
    signal_logs: list[tuple[int, str, str, str]]  # (相对故障开始的分钟数, 服务名, 级别, 内容)
    root_cause: str
    keywords: list[str]
    must_have: list[str]  # 本次事故特有的事实（见 app/scenario.py 的 GroundTruth.must_have）
    references: list[str]  # 好报告应当引用的原文
    culprit_deploy: dict | None = None  # {"summary", "diff_summary", "minutes_before"}
    runbook_title: str = ""
    runbook_content: str = ""
    extra_deploys: list[dict] = field(default_factory=list)


def sha(seed: str) -> str:
    return hashlib.sha1(seed.encode()).hexdigest()[:7]


# ---------------------------------------------------------------------------
# 10 种根因。每个函数接收一个变体的参数，返回 Parts。
# ---------------------------------------------------------------------------
def connection_pool(p: dict) -> Parts:
    s, key = p["svc"], p["key"]
    pool = p["new"]
    return Parts(
        title=f"{s} intermittently returns HTTP 500",
        description="Customers report random failures that started around 2pm UTC. Retrying sometimes works.",
        signal_logs=[
            (-2, s, "INFO", f"Application started version={p['version']} {key}={pool}"),
            (2, s, "ERROR", f"{p['pool_name']} - Connection is not available, request timed out after 30000ms (total={pool}, active={pool}, idle=0, waiting={p['waiting']})"),
            (3, s, "ERROR", "Request failed status=500 cause=SQLTransientConnectionException"),
            (9, s, "ERROR", f"{p['pool_name']} - Connection is not available, request timed out after 30000ms (total={pool}, active={pool}, idle=0, waiting={p['waiting'] + 11})"),
            (10, s, "ERROR", "Request failed status=500 cause=SQLTransientConnectionException"),
        ],
        root_cause=f"Deploy {p['version']} reduced the database connection pool ({key}) from {p['old']} to {pool}, so the pool is exhausted under normal load",
        keywords=["connection pool|pool size|pool exhaust", key.split(".")[-1].lower() + "|pool size|maximum size|max size|pool max", p["version"] + "|" + sha(f"{s}-{p['version']}")],
        must_have=[key.split(".")[-1].lower() + "|pool size|maximum size|max size|" + p["version"] + "|" + sha(f"{s}-{p['version']}")],
        references=[sha(f"{s}-{p['version']}"), "Connection is not available, request timed out after 30000ms"],
        culprit_deploy={
            "version": p["version"],
            "summary": "Reduce idle database connection cost",
            "diff_summary": f"application.yml: {key} {p['old']} -> {pool}",
            "minutes_before": 5,
        },
        runbook_title="Intermittent 5xx with database errors",
        runbook_content="Possible causes: connection pool too small for the traffic, long-running transactions holding connections, database failover, network problems between the service and the database. Check pool metrics, recent configuration changes and database health.",
    )


def null_pointer(p: dict) -> Parts:
    s, cls = p["svc"], p["cls"]
    return Parts(
        title=f"{s} fails for some orders after this afternoon",
        description="Support reports that a subset of customers cannot complete their purchase. Others are fine.",
        signal_logs=[
            (1, s, "ERROR", f"Unhandled exception in {p['endpoint']}: java.lang.NullPointerException: Cannot invoke \"{p['field']}\" because \"{p['var']}\" is null"),
            (1, s, "ERROR", f"\tat com.acme.{s.replace('-', '')}.{cls}.apply({cls}.java:{p['line']})"),
            (4, s, "ERROR", f"Request failed status=500 cause=NullPointerException in {cls}"),
            (8, s, "WARN", f"Request with {p['trigger']} failed; requests without it succeeded"),
            (12, s, "ERROR", f"Unhandled exception in {p['endpoint']}: java.lang.NullPointerException in {cls}.apply"),
        ],
        root_cause=f"Release {p['version']} introduced a null pointer bug in {cls}: requests containing {p['trigger']} hit an unchecked null",
        keywords=["nullpointerexception|null pointer", cls.lower(), p["version"] + "|" + sha(f"{s}-{p['version']}")],
        must_have=[cls.lower() + "|" + p["version"] + "|" + sha(f"{s}-{p['version']}")],
        references=[sha(f"{s}-{p['version']}"), "NullPointerException"],
        culprit_deploy={
            "version": p["version"],
            "summary": p["summary"],
            "diff_summary": f"{cls}.java: {p['diff']}",
            "minutes_before": 8,
        },
        runbook_title="Errors affecting only some requests",
        runbook_content="Possible causes: a code regression in a recent release, bad input from one client, a feature flag, data-dependent bugs. Compare failing and succeeding requests, check the stack trace and the latest release notes.",
    )


def memory_leak(p: dict) -> Parts:
    s = p["svc"]
    return Parts(
        title=f"{s} keeps restarting and is slow before each restart",
        description="On-call sees the service restart every 20 to 30 minutes. Latency climbs between restarts.",
        signal_logs=[
            (0, s, "INFO", f"Application started version={p['version']} heap.max=2048MB"),
            (6, s, "WARN", "GC pause 1400ms heap used=78%"),
            (14, s, "WARN", "GC pause 3100ms heap used=91%"),
            (19, s, "ERROR", "java.lang.OutOfMemoryError: Java heap space"),
            (20, s, "ERROR", "Container terminated reason=OOMKilled restart_count=4"),
            (21, s, "INFO", f"Application started version={p['version']} heap.max=2048MB"),
        ],
        root_cause=f"Release {p['version']} added an in-memory {p['cache']} without any eviction, so heap grows until OutOfMemoryError kills the container",
        keywords=["outofmemory|out of memory|oom|heap", "cache", "eviction|unbounded|no limit|without limit|never expire"],
        must_have=[p["cache_class"].lower() + "|" + p["cache"] + "|" + p["version"] + "|" + sha(f"{s}-{p['version']}")],
        references=[sha(f"{s}-{p['version']}"), "java.lang.OutOfMemoryError: Java heap space"],
        culprit_deploy={
            "version": p["version"],
            "summary": f"Speed up lookups with a local {p['cache']}",
            "diff_summary": f"{p['cache_class']}.java: new ConcurrentHashMap cache, no size limit or expiry",
            "minutes_before": 25,
        },
        runbook_title="Service restarts repeatedly",
        runbook_content="Possible causes: memory leak or unbounded cache, failing health checks, crash loops from bad configuration, node pressure. Look at heap trends, OOM events and what changed in the last release.",
    )


def dependency_timeout(p: dict) -> Parts:
    s, d = p["svc"], p["dep"]
    return Parts(
        title=f"{s} is very slow and times out",
        description="Pages that depend on this service take more than 30 seconds or fail since about 2pm UTC.",
        signal_logs=[
            (1, s, "WARN", f"Call to {d} took 4800ms (threshold 1000ms)"),
            (3, s, "ERROR", f"Timeout calling {d} after 5000ms"),
            (4, d, "WARN", f"{p['dep_problem']}"),
            (6, s, "ERROR", f"Circuit breaker OPEN for {d}, failing fast"),
            (7, s, "ERROR", f"Request failed status=503 cause=CallNotPermittedException for {d}"),
            (9, d, "ERROR", f"{p['dep_problem']} (queue depth 912)"),
        ],
        root_cause=f"The downstream {d} became slow ({p['dep_problem']}), calls from {s} timed out and the circuit breaker opened",
        keywords=[d, "timeout|timed out|timing out|times out|slow|unresponsive", "circuit breaker|circuit-breaker|lock wait|thread pool|provider|latency|exhaust|queued"],
        must_have=[d],
        references=[f"Timeout calling {d} after 5000ms", f"Circuit breaker OPEN for {d}"],
        runbook_title="High latency and timeouts",
        runbook_content="Possible causes: a slow dependency, a recent deploy of this service, resource saturation, network issues. Check dependency latency first, then recent deploys, then CPU and memory.",
    )


def expired_certificate(p: dict) -> Parts:
    s = p["svc"]
    return Parts(
        title=f"{s} cannot reach {p['peer']} since midnight UTC",
        description=f"All calls from this service to {p['peer']} fail. Nothing was released recently.",
        signal_logs=[
            (-3, s, "INFO", f"Connected to {p['peer']} (tls)"),
            (0, s, "ERROR", f"javax.net.ssl.SSLHandshakeException: PKIX path validation failed: java.security.cert.CertPathValidatorException: validity check failed"),
            (0, s, "ERROR", f"Certificate for {p['peer']} expired on {p['expiry']} (NotAfter={p['expiry']})"),
            (5, s, "ERROR", f"Request to {p['peer']} failed: SSL handshake failure"),
            (11, s, "ERROR", f"Request to {p['peer']} failed: SSL handshake failure"),
        ],
        root_cause=f"The TLS certificate used for {p['peer']} expired on {p['expiry']}, so every handshake fails",
        keywords=["certificate|cert ", "expired|expiry|expire", "tls|ssl|handshake"],
        must_have=[p["expiry"][:10] + "|notafter|not after|expired on|expiry date|expiration date|handshake exception|handshake failure|pkix"],
        references=[f"Certificate for {p['peer']} expired on {p['expiry']}", "SSLHandshakeException"],
        runbook_title="Connection failures to an upstream service",
        runbook_content="Possible causes: expired or untrusted certificates, DNS problems, firewall changes, the upstream being down. Check the error type: handshake errors point to certificates, connection refused points to the network.",
    )


def disk_full(p: dict) -> Parts:
    s = p["svc"]
    return Parts(
        title=f"{s} nodes report errors writing files and then crash",
        description="Several instances became unhealthy within an hour. Disk alerts fired on the hosts.",
        signal_logs=[
            (0, s, "INFO", f"Application started version={p['version']} log.level=DEBUG"),
            (10, s, "WARN", "Disk usage 87% on /var/log"),
            (24, s, "ERROR", "java.io.IOException: No space left on device"),
            (25, s, "ERROR", "Failed to write to /var/log/app/app.log: No space left on device"),
            (27, s, "ERROR", "Health check failed: cannot write temp file"),
        ],
        root_cause=f"Release {p['version']} switched the log level from INFO to DEBUG, log files filled the disk and writes began to fail",
        keywords=["debug", "log level|logging|log volume|verbose", "disk|space"],
        must_have=["debug|" + sha(f"{s}-{p['version']}")],
        references=[sha(f"{s}-{p['version']}"), "No space left on device"],
        culprit_deploy={
            "version": p["version"],
            "summary": "Improve troubleshooting output",
            "diff_summary": "logback.xml: root level INFO -> DEBUG",
            "minutes_before": 30,
        },
        runbook_title="Disk space alerts and write failures",
        runbook_content="Possible causes: runaway log volume, large temp files, a stuck log rotation, data growth. Check which directory grew, the log level, and whether rotation is working.",
    )


def secret_rotation(p: dict) -> Parts:
    s, t = p["svc"], p["target"]
    return Parts(
        title=f"{s} gets rejected by {t}",
        description=f"Requests from this service to {t} started failing at about 1:05pm UTC. No release happened.",
        signal_logs=[
            (-4, s, "INFO", f"Calling {t} ok status=200"),
            (0, "secrets-manager", "INFO", f"Secret {p['secret']} rotated version={p['v']} by rotation-job"),
            (1, s, "ERROR", f"{t} returned 401 Unauthorized: invalid credentials"),
            (3, s, "ERROR", f"{t} returned 401 Unauthorized: invalid credentials"),
            (8, s, "WARN", "Cached credential age=2h12m"),
            (12, s, "ERROR", f"Request failed status=502 cause=UpstreamAuthenticationFailed for {t}"),
        ],
        root_cause=f"Secret {p['secret']} was rotated but {s} kept using its cached old credential, so {t} answers 401",
        keywords=["rotat", "credential|secret|api key|token|password", "401|unauthorized|authenticat"],
        must_have=[p["secret"] + "|secrets-manager|rotation-job|cached|stale|old credential"],
        references=[f"Secret {p['secret']} rotated", f"{t} returned 401 Unauthorized: invalid credentials"],
        runbook_title="Authentication failures to an upstream",
        runbook_content="Possible causes: rotated or revoked credentials, clock skew, permission changes, expired tokens. Check when the failures started and whether any secret or permission changed at that time.",
    )


def traffic_spike(p: dict) -> Parts:
    s = p["svc"]
    return Parts(
        title=f"{s} returns many errors and is hard to reach",
        description="Dashboards show high error counts since 2pm UTC. No change was released.",
        signal_logs=[
            (0, s, "INFO", f"Request rate 610 rps"),
            (2, s, "WARN", f"Request rate 4800 rps referrer={p['campaign']}"),
            (4, s, "ERROR", "Rate limit exceeded for tenant=public status=429"),
            (6, s, "ERROR", "Worker queue full (size=2000), rejecting requests"),
            (9, s, "ERROR", "Request failed status=503 cause=RejectedExecutionException"),
            (12, s, "WARN", "CPU usage 98% on all instances"),
        ],
        root_cause=f"A sudden traffic spike from {p['campaign']} (about 8x normal load) saturated {s}, triggering rate limits and a full worker queue",
        keywords=["traffic|load|request rate|rps", "spike|surge|burst|increase", "rate limit|429|saturat"],
        must_have=[p["campaign"] + "|4800|8x|eight times|worker queue|queue full|size=2000|rejectedexecution|tenant"],
        references=["Request rate 4800 rps", "Rate limit exceeded for tenant"],
        runbook_title="High error rate with saturated resources",
        runbook_content="Possible causes: traffic surge, a retry storm, a regression that increases CPU per request, too few instances. Compare request rate with the baseline and look at autoscaling.",
    )


def slow_query(p: dict) -> Parts:
    s = p["svc"]
    return Parts(
        title=f"{s} responses became very slow after this morning",
        description="Latency went from about 100ms to over 8 seconds. Error rate is only slightly higher.",
        signal_logs=[
            (3, s, "WARN", f"Slow query 8200ms: SELECT * FROM {p['table']} WHERE {p['col']} = ?"),
            (5, s, "WARN", f"Slow query 9100ms: SELECT * FROM {p['table']} WHERE {p['col']} = ?"),
            (7, "database", "WARN", f"Sequential scan on {p['table']} rows=4200000 filter={p['col']}"),
            (10, s, "ERROR", "Request timed out after 10000ms waiting for query"),
            (13, s, "WARN", f"Slow query 11000ms: SELECT * FROM {p['table']} WHERE {p['col']} = ?"),
        ],
        root_cause=f"Migration {p['migration']} dropped the index on {p['table']}.{p['col']}, so lookups do full table scans",
        keywords=["index", "migration|schema|dropp|removed|missing index", p["col"]],
        must_have=[p["col"] + "|" + p["migration"].lower() + "|sequential scan|seq scan|full table scan|full scan|" + sha(f"{s}-{p['version']}")],
        references=[sha(f"{s}-{p['version']}"), f"Sequential scan on {p['table']}"],
        culprit_deploy={
            "version": p["version"],
            "summary": "Clean up unused database objects",
            "diff_summary": f"{p['migration']}.sql: DROP INDEX idx_{p['table']}_{p['col']}",
            "minutes_before": 20,
        },
        runbook_title="Slow responses with database queries",
        runbook_content="Possible causes: a missing or dropped index, lock contention, table growth, a bad query plan, an overloaded database. Inspect slow query logs and query plans and check recent schema migrations.",
    )


def feature_flag(p: dict) -> Parts:
    s, flag = p["svc"], p["flag"]
    return Parts(
        title=f"{s} errors for customers in some countries",
        description="Only some customers see failures, mostly outside the main market. No release happened today.",
        signal_logs=[
            (-2, "config-service", "INFO", f"Feature flag {flag} enabled for 100% of traffic by user=ops-bot"),
            (1, s, "ERROR", f"{p['engine']} failed: {p['failure']}"),
            (3, s, "ERROR", f"Request failed status=500 cause={p['engine']}Exception"),
            (6, s, "ERROR", f"{p['engine']} failed: {p['failure']}"),
            (9, s, "WARN", f"Fallback path disabled by flag {flag}"),
        ],
        root_cause=f"Feature flag {flag} was switched on for all traffic and routes requests through {p['engine']}, which fails for {p['failure']}",
        keywords=["feature flag|flag", flag, p["engine"].lower()],
        must_have=[flag + "|" + p["engine"].lower()],
        references=[f"Feature flag {flag} enabled for 100% of traffic", f"{p['engine']} failed: {p['failure']}"],
        runbook_title="Errors affecting a subset of customers",
        runbook_content="Possible causes: a feature flag or experiment change, region-specific data, a partial rollout of a release, a dependency that is unhealthy in one region. Check the flag audit log and compare affected and unaffected customers.",
    )


# ---------------------------------------------------------------------------
# 留出集（holdout）：另外 3 种"根因在别的服务里的非发布类变更"。
# 开发时用 30 个开发场景反复调 agent；这几个是调完之后才用来检验的：
# agent 的改进如果只是"背会了开发集里的题型"，在这里会露馅；如果真的学会了去查相关服务，这里也会变好。
# 所以它们的题型是开发集里没有出现过的新类型，开发 agent 的时候不要看它们的答案，也不要拿它们调提示词。
# ---------------------------------------------------------------------------
def autoscaler_limit(p: dict) -> Parts:
    s = p["svc"]
    return Parts(
        title=f"{s} is slow and returns errors during the evening peak",
        description="Latency and errors rise every evening when traffic is highest. Nothing was released.",
        signal_logs=[
            (-3, "autoscaler", "INFO", f"HPA {s} maxReplicas changed {p['old']} -> {p['new']} by user=ops-bot"),
            (4, s, "WARN", "CPU usage 97% on all instances"),
            (6, s, "ERROR", "no healthy upstream: all instances saturated"),
            (9, s, "ERROR", "Request failed status=503 cause=UpstreamOverloaded"),
            (12, "autoscaler", "WARN", f"HPA {s} wants {p['old']} replicas but is capped at {p['new']}"),
        ],
        root_cause=f"The autoscaler limit for {s} was lowered from {p['old']} to {p['new']} replicas, so it cannot scale out during the evening peak and instances saturate",
        keywords=["autoscal|replica|hpa|scaling", "limit|cap|maxreplicas|lowered|reduced|changed", "cpu|saturat|capacity|overload"],
        must_have=["maxreplicas|replicas|hpa|autoscaler"],
        references=[f"HPA {s} maxReplicas changed {p['old']} -> {p['new']}", "CPU usage 97% on all instances"],
        runbook_title="Slow responses and errors at peak hours",
        runbook_content="Possible causes: traffic growth, a slow dependency, noisy neighbours on the node, insufficient capacity, resource limits set too low. Compare CPU and latency with the baseline and check recent configuration changes.",
    )


def dns_change(p: dict) -> Parts:
    s, host = p["svc"], p["host"]
    return Parts(
        title=f"{s} cannot reach one of its backends since the afternoon",
        description="Calls to a backend system started timing out at about 2pm UTC for every request. No release happened.",
        signal_logs=[
            (-2, "dns-service", "INFO", f"Record {host} A changed {p['old']} -> {p['new']} by terraform-apply"),
            (1, s, "ERROR", f"Connect to {host} ({p['new']}) timed out after 3000ms"),
            (3, s, "ERROR", "java.net.SocketTimeoutException: connect timed out"),
            (7, s, "ERROR", f"Connect to {host} ({p['new']}) timed out after 3000ms"),
            (10, s, "ERROR", "Request failed status=504 cause=BackendUnavailable"),
        ],
        root_cause=f"The DNS record for {host} was changed from {p['old']} to {p['new']} by a terraform apply, so {s} connects to the wrong address and times out",
        keywords=["dns|record|a record|ip address", "changed|updated|repointed|wrong|stale|new address", "timed out|timeout|unreachable"],
        must_have=[f"{p['new']}|terraform|{host}"],
        references=[f"Record {host} A changed {p['old']} -> {p['new']}", f"Connect to {host} ({p['new']}) timed out"],
        runbook_title="Timeouts when calling a backend",
        runbook_content="Possible causes: the backend is down or overloaded, a firewall or routing change, a wrong address resolved for the backend, network congestion. Check where the connection attempts go and whether the backend itself is healthy.",
    )


def network_policy(p: dict) -> Parts:
    s, dep = p["svc"], p["dep"]
    return Parts(
        title=f"{s} cannot call {dep} since 3pm",
        description=f"Every call from this service to {dep} fails. No release happened and {dep} itself looks healthy.",
        signal_logs=[
            (-2, "network-policy-controller", "INFO", f"NetworkPolicy deny-egress-default applied to namespace {p['ns']} by user=sec-bot"),
            (1, s, "ERROR", f"Connection to {dep}:443 timed out (no route to host)"),
            (4, s, "ERROR", f"Connection to {dep}:443 timed out (no route to host)"),
            (8, s, "ERROR", f"Request failed status=502 cause=UpstreamUnreachable for {dep}"),
            (11, dep, "INFO", "Health check ok, no inbound traffic from the last 10 minutes"),
        ],
        root_cause=f"A default-deny egress NetworkPolicy was applied to namespace {p['ns']}, blocking {s} from reaching {dep}",
        keywords=["network policy|networkpolicy|egress|firewall", "deny|block|applied|restrict", "timed out|timeout|unreachable|no route"],
        must_have=["deny-egress|networkpolicy|network policy|sec-bot|egress"],
        references=[f"NetworkPolicy deny-egress-default applied to namespace {p['ns']}", f"Connection to {dep}:443 timed out"],
        runbook_title="Cannot reach an upstream service",
        runbook_content="Possible causes: the upstream is down, DNS problems, certificate errors, routing or firewall changes, security group rules. Check whether the upstream is healthy and whether anything about connectivity changed recently.",
    )


# 每个原型：(名称, 类别, 函数, 三个变体的参数)。变体 0/1/2 = 简单/中等/困难。
ARCHETYPES = [
    ("connection_pool", "connection_pool_exhaustion", connection_pool, [
        dict(svc="orders-api", key="spring.datasource.hikari.maximumPoolSize", old=40, new=4, version="3.8.0", pool_name="HikariPool-1", waiting=22),
        dict(svc="billing-service", key="hikari.maximumPoolSize", old=30, new=3, version="1.19.2", pool_name="HikariPool-2", waiting=15),
        dict(svc="profile-api", key="datasource.maxActive", old=25, new=2, version="7.2.0", pool_name="TomcatPool", waiting=30),
    ]),
    ("null_pointer", "code_regression", null_pointer, [
        dict(svc="checkout-api", cls="PromoService", version="4.1.0", endpoint="POST /checkout", field="com.acme.Promo.getRules()", var="promo", line=88, trigger="a promo code", summary="Add stackable discount logic", diff="new discount stacking logic"),
        dict(svc="cart-service", cls="CouponValidator", version="2.6.1", endpoint="POST /cart/apply", field="java.lang.String.length()", var="coupon.region", line=57, trigger="a regional coupon", summary="Support regional coupons", diff="regional coupon validation added"),
        dict(svc="pricing-api", cls="TaxCalculator", version="5.0.3", endpoint="GET /price", field="com.acme.TaxTable.rate()", var="table", line=141, trigger="a shipping address outside the EU", summary="Refactor tax calculation", diff="tax table lookup refactored"),
    ]),
    ("memory_leak", "memory_leak", memory_leak, [
        dict(svc="catalog-api", version="6.3.0", cache="product cache", cache_class="ProductCache"),
        dict(svc="search-service", version="2.2.4", cache="query result cache", cache_class="ResultCache"),
        dict(svc="recommendation-service", version="9.1.0", cache="user embedding cache", cache_class="EmbeddingCache"),
    ]),
    ("dependency_timeout", "slow_dependency", dependency_timeout, [
        dict(svc="orders-api", dep="inventory-service", dep_problem="database lock wait timeout exceeded"),
        dict(svc="checkout-api", dep="payment-gateway", dep_problem="upstream provider latency 6200ms"),
        dict(svc="shipping-api", dep="rates-service", dep_problem="thread pool exhausted, requests queued"),
    ]),
    ("expired_certificate", "expired_certificate", expired_certificate, [
        dict(svc="payments-api", peer="bank-partner-gateway", expiry="2026-08-31T23:59:59Z"),
        dict(svc="notification-worker", peer="sms-provider", expiry="2026-09-01T00:00:00Z"),
        dict(svc="reporting-service", peer="data-warehouse", expiry="2026-09-01T00:00:00Z"),
    ]),
    ("disk_full", "log_volume_disk_full", disk_full, [
        dict(svc="auth-service", version="8.4.0"),
        dict(svc="gateway-api", version="3.3.1"),
        dict(svc="media-service", version="1.7.5"),
    ]),
    ("secret_rotation", "credential_rotation", secret_rotation, [
        dict(svc="orders-api", target="payments-api", secret="payments-api-key", v=7),
        dict(svc="email-service", target="mail-relay", secret="mail-relay-password", v=12),
        dict(svc="analytics-worker", target="metrics-ingest", secret="ingest-token", v=3),
    ]),
    ("traffic_spike", "traffic_spike", traffic_spike, [
        dict(svc="storefront-api", campaign="flash-sale-newsletter"),
        dict(svc="search-api", campaign="partner-widget-launch"),
        dict(svc="coupon-service", campaign="social-media-promo"),
    ]),
    ("slow_query", "missing_index", slow_query, [
        dict(svc="orders-api", table="orders", col="customer_id", migration="V45__cleanup", version="3.9.0"),
        dict(svc="inventory-service", table="stock_items", col="warehouse_id", migration="V112__tidy_schema", version="4.0.2"),
        dict(svc="invoice-api", table="invoices", col="account_id", migration="V23__drop_unused", version="1.12.0"),
    ]),
    ("feature_flag", "feature_flag_change", feature_flag, [
        dict(svc="pricing-api", flag="new-pricing-engine", engine="PricingEngineV2", failure="unsupported currency JPY"),
        dict(svc="checkout-api", flag="express-checkout-v2", engine="ExpressCheckout", failure="missing tax region for CA-QC"),
        dict(svc="search-api", flag="semantic-ranking", engine="SemanticRanker", failure="empty embedding for non-latin queries"),
    ]),
]

# 留出集的原型：每个只有 2 个变体（中等、困难）。
HOLDOUT_ARCHETYPES = [
    ("autoscaler_limit", "autoscaler_limit_lowered", autoscaler_limit, [
        dict(svc="checkout-api", old=12, new=2),
        dict(svc="search-service", old=20, new=3),
    ]),
    ("dns_change", "dns_record_changed", dns_change, [
        dict(svc="orders-api", host="orders-db.internal", old="10.2.0.5", new="10.9.9.9"),
        dict(svc="reporting-service", host="warehouse.internal", old="10.4.1.8", new="10.8.8.8"),
    ]),
    ("network_policy", "network_policy_blocked", network_policy, [
        dict(svc="payments-api", dep="bank-partner-gateway", ns="payments"),
        dict(svc="notification-worker", dep="sms-provider", ns="notifications"),
    ]),
]

DIFFICULTY = ["easy", "medium", "hard"]
INNOCENT_DEPLOYS = [
    ("Update copyright year in footer", "templates/footer.html: 1 line changed"),
    ("Bump logging library", "pom.xml: logback 1.4.11 -> 1.4.14"),
    ("Fix typo in order confirmation email", "templates/order_confirmation.html: 1 line changed"),
    ("Update API documentation", "docs/openapi.yaml: description updates"),
]
NOISE_LOGS = [
    (-25, "INFO", "Order placed order_id={n} total=42.10"),
    (-14, "INFO", "Cache hit ratio 0.91 for key prefix=catalog"),
    (-6, "WARN", "Slow response from payment gateway latency_ms=1800"),
    (4, "INFO", "Order placed order_id={n2} total=15.00"),
    (7, "WARN", "Cache miss ratio 0.42 for key prefix=catalog"),
    (11, "INFO", "Charge succeeded charge_id=ch_{n3} amount=42.10"),
]
GENERIC_RUNBOOK = ("RB-007", "General HTTP 5xx investigation checklist",
    "1. Find the first error in the logs and note the time. 2. Check recent deploys of the affected service near that time. 3. Check dependencies (database, payment gateway, cache). 4. Compare error rate before and after the change.")
IRRELEVANT_RUNBOOK = ("RB-210", "Rotating the CDN signing key",
    "Steps to rotate the CDN signing key every quarter: generate a new key, publish it, wait for propagation, retire the old key.")


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_scenario(
    index: int, archetype_idx: int, variant: int, archetype: tuple | None = None, params_idx: int | None = None, split: str = "dev"
) -> tuple[str, dict, dict]:
    """返回 (场景名, 场景 JSON, 清单里的一项)。index 是全局序号，决定日期和编号。

    variant 决定难度（0 简单 / 1 中等 / 2 困难）；params_idx 是它在该原型的参数列表里的位置（留出集的原型只有 2 个变体）。
    """
    name, category, fn, variants = archetype or ARCHETYPES[archetype_idx]
    params = variants[variant if params_idx is None else params_idx]
    parts = fn(params)
    bug_number = FIRST_BUG_NUMBER + index
    ticket_id = f"BUG-{bug_number}"
    service = params["svc"]
    t0 = BASE_DAY + timedelta(days=index)  # 故障开始的时间

    # --- 日志：信号 + 噪声。噪声每个场景都有，信号只占一小部分 ---
    logs = [(t0 + timedelta(minutes=m), svc, lvl, msg) for m, svc, lvl, msg in parts.signal_logs]
    for offset, level, template in NOISE_LOGS:
        text = template.format(n=88000 + index * 7, n2=88100 + index * 7, n3=9900 + index)
        logs.append((t0 + timedelta(minutes=offset), service, level, text))
    if variant >= 1:  # 中等及以上：多一些看起来吓人、其实一直存在的警告
        logs.append((t0 + timedelta(minutes=-9), service, "WARN", "Retrying idempotent call attempt=2"))
        logs.append((t0 + timedelta(minutes=5), service, "WARN", "Deprecated API v1 endpoint called by legacy client"))
    logs.sort(key=lambda entry: entry[0])

    # --- 发布：元凶（如果有）+ 无辜的发布 + 别的服务的发布 ---
    deploys = []
    references = list(parts.references)
    if parts.culprit_deploy:
        d = parts.culprit_deploy
        deploys.append({
            "timestamp": _iso(t0 - timedelta(minutes=d["minutes_before"])),
            "service": service, "version": d["version"], "commit": sha(f"{service}-{d['version']}"),
            "author": "kpatel", "summary": d["summary"], "diff_summary": d["diff_summary"],
        })
    summary, diff = INNOCENT_DEPLOYS[index % len(INNOCENT_DEPLOYS)]
    deploys.append({
        "timestamp": _iso(t0 - timedelta(days=5)), "service": service, "version": "0.0.1-old",
        "commit": sha(f"{service}-old-{index}"), "author": "dlee", "summary": "Routine dependency update",
        "diff_summary": "pom.xml: minor version bumps",
    })
    if variant == 2:
        # 困难：故障前不久有一次无辜的发布。对没有元凶发布的场景，它是干扰项；
        # 对有元凶的场景，它排在元凶【之后】，所以"怪最近一次发布"会怪错。
        innocent_minutes = 3 if not parts.culprit_deploy else max(1, parts.culprit_deploy["minutes_before"] - 3)
        deploys.append({
            "timestamp": _iso(t0 - timedelta(minutes=innocent_minutes)),
            "service": service, "version": "99.0.0-docs", "commit": sha(f"{service}-innocent-{index}"),
            "author": "mchen", "summary": summary, "diff_summary": diff,
        })
    deploys.append({
        "timestamp": _iso(t0 - timedelta(hours=3)), "service": "payments-service", "version": "5.2.1",
        "commit": sha(f"payments-{index}"), "author": "mchen", "summary": "Bump logging library",
        "diff_summary": "pom.xml: logback 1.4.11 -> 1.4.14",
    })

    runbooks = [
        {"id": f"RB-{300 + archetype_idx}", "title": parts.runbook_title, "content": parts.runbook_content},
        {"id": GENERIC_RUNBOOK[0], "title": GENERIC_RUNBOOK[1], "content": GENERIC_RUNBOOK[2]},
        {"id": IRRELEVANT_RUNBOOK[0], "title": IRRELEVANT_RUNBOOK[1], "content": IRRELEVANT_RUNBOOK[2]},
    ]
    # 有人报告故障，一定发生在第一条错误日志之后（至少 5 分钟，通常 20 分钟）。
    first_error_offset = min(m for m, _, lvl, _ in parts.signal_logs if lvl == "ERROR")
    reported_after = max(20, first_error_offset + 5)
    scenario = {
        "ticket": {
            "id": ticket_id, "title": parts.title, "description": parts.description, "service": service,
            "severity": ["medium", "high", "high"][variant], "reported_at": _iso(t0 + timedelta(minutes=reported_after)),
        },
        "logs": [{"timestamp": _iso(ts), "service": svc, "level": lvl, "message": msg} for ts, svc, lvl, msg in logs],
        "deploys": deploys,
        "runbooks": runbooks,
        "ground_truth": {
            "root_cause": parts.root_cause, "keywords": parts.keywords, "must_have": parts.must_have, "category": category,
            "culprit_references": references,
        },
    }
    entry = {
        "scenario": ticket_id.lower(), "category": category, "difficulty": DIFFICULTY[variant],
        "has_culprit_deploy": parts.culprit_deploy is not None,
        "split": split,
    }
    return ticket_id.lower(), scenario, entry


def build_all() -> tuple[dict[str, dict], list[dict]]:
    """返回 ({场景名: 场景 JSON}, 清单)。顺序固定：先所有原型的简单变体，再中等，再困难。"""
    scenarios: dict[str, dict] = {}
    manifest: list[dict] = []
    index = 0
    for variant in range(3):
        for archetype_idx in range(len(ARCHETYPES)):
            name, scenario, entry = build_scenario(index, archetype_idx, variant)
            scenarios[name] = scenario
            manifest.append(entry)
            index += 1
    # 留出集接在后面：编号 BUG-2031 起，split = "holdout"
    for offset, archetype in enumerate(HOLDOUT_ARCHETYPES):
        for params_idx, variant in enumerate((1, 2)):
            # archetype_idx 只用来给 runbook 编号（RB-310...），让留出集的 runbook 编号不和开发集的重复
            name, scenario, entry = build_scenario(
                index, len(ARCHETYPES) + offset, variant, archetype=archetype, params_idx=params_idx, split="holdout"
            )
            scenarios[name] = scenario
            manifest.append(entry)
            index += 1
    return scenarios, manifest
