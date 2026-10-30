"""Jira 模拟器：扮演 Jira，向我们的 Webhook 接口发送签名的事件。

运行（后端和数据库要先启动）：
    .venv/bin/python scripts/simulate_jira.py created --wait

没有真实的 Jira 账号也能完整演示：签名验证、去重、重试这些逻辑都是真的，只是触发源是模拟的。

可以模拟的场景（括号里是服务端应该返回的状态码）：
    created          新建 Bug，签名正确                            -> 202 已入队
    duplicate        同一个事件连发两次（模拟 Jira 重试）            -> 202，然后 200（去重）
    bad-signature    用错误的密钥签名（模拟伪造的请求）              -> 401
    tampered         内容被改过，但签名是原内容的                    -> 401
    no-signature     完全没有签名头                                -> 401
    wrong-method     签名方法是 sha1 而不是 sha256                 -> 401
    non-bug          新建的是 Task 不是 Bug                        -> 200 忽略
    updated          事件类型是"更新"不是"新建"                    -> 200 忽略
    unknown-ticket   工单号没有对应的调查数据                       -> 200 忽略

密钥从 .env 的 JIRA_WEBHOOK_SECRET 读取，【不会打印出来】。
"""

import argparse
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable

import httpx

import sys
from pathlib import Path

# 直接运行 `python scripts/xxx.py` 时，Python 的模块搜索路径里是【脚本所在的目录】（scripts/），
# 不是项目根目录，所以 `import app` 会失败。把项目根目录加进去，脚本在任何目录下都能运行。
# （之前我只在设置了 PYTHONPATH 的环境里试过，等于替用户掩盖了这个问题。）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SIGNATURE_HEADER = "X-Hub-Signature"
DELIVERY_ID_HEADER = "X-Atlassian-Webhook-Identifier"
KINDS = [
    "created",
    "duplicate",
    "bad-signature",
    "tampered",
    "no-signature",
    "wrong-method",
    "non-bug",
    "updated",
    "unknown-ticket",
]


def build_event(ticket: str = "BUG-1042", issue_type: str = "Bug", event: str = "jira:issue_created") -> bytes:
    """按 Jira Cloud 的格式构造事件。真实事件里有更多字段，我们只关心其中几个，其余服务端会忽略。"""
    payload = {
        "timestamp": int(time.time() * 1000),
        "webhookEvent": event,
        "issue": {
            "id": "10001",
            "key": ticket,
            "fields": {
                "summary": "Checkout API intermittently returns HTTP 500",
                "issuetype": {"name": issue_type},
                "project": {"key": ticket.split("-")[0]},
            },
        },
        "user": {"displayName": "Jira Simulator"},
    }
    return json.dumps(payload).encode("utf-8")


def sign(secret: str, body: bytes, method: str = "sha256") -> str:
    """和真实 Jira 一样：用共享密钥对【请求体的原始字节】做 HMAC-SHA256，放进 X-Hub-Signature。"""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{method}={digest}"


def send(
    client: httpx.Client, url: str, body: bytes, signature: str | None, delivery_id: str
) -> httpx.Response:
    headers = {"Content-Type": "application/json", DELIVERY_ID_HEADER: delivery_id}
    if signature is not None:
        headers[SIGNATURE_HEADER] = signature
    # content=body：发送的就是我们签名时用的那一份字节，一个字节都不能变。
    return client.post(f"{url}/webhooks/jira", content=body, headers=headers)


def _plan(kind: str, secret: str, ticket: str, delivery_id: str) -> list[tuple[str, bytes, str | None, int]]:
    """每个场景要发的请求列表：(描述, 请求体, 签名, 期望状态码)。"""
    good = build_event(ticket)
    if kind == "created":
        return [("signed Bug-created event", good, sign(secret, good), 202)]
    if kind == "duplicate":
        return [
            ("first delivery", good, sign(secret, good), 202),
            ("redelivery of the same event", good, sign(secret, good), 200),
        ]
    if kind == "bad-signature":
        return [("signed with the wrong secret", good, sign("not-the-real-secret", good), 401)]
    if kind == "tampered":
        other = build_event("BUG-9999")
        return [("body changed after signing", other, sign(secret, good), 401)]
    if kind == "no-signature":
        return [("no signature header", good, None, 401)]
    if kind == "wrong-method":
        return [("sha1 instead of sha256", good, sign(secret, good, method="sha1"), 401)]
    if kind == "non-bug":
        body = build_event(ticket, issue_type="Task")
        return [("a Task, not a Bug", body, sign(secret, body), 200)]
    if kind == "updated":
        body = build_event(ticket, event="jira:issue_updated")
        return [("an update event", body, sign(secret, body), 200)]
    if kind == "unknown-ticket":
        body = build_event("BUG-7777")
        return [("ticket without investigation data", body, sign(secret, body), 200)]
    raise ValueError(f"unknown scenario: {kind}")


def wait_for_job(
    client: httpx.Client,
    url: str,
    job_id: str,
    out: Callable[[str], None],
    timeout: float = 180,
    interval: float = 2.0,
) -> dict | None:
    """轮询任务状态直到结束，打印每次状态变化。这就是"取餐窗口"：用 202 响应里的 job_id 来取结果。"""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        job = client.get(f"{url}/jobs/{job_id}").json()
        if job["status"] != last:
            last = job["status"]
            out(f"    job {job_id[:8]}… is now {last} (attempt {job['attempts']}/{job['max_attempts']})")
        if last in ("succeeded", "failed"):
            return job
        time.sleep(interval)
    out("    gave up waiting; the job is still not finished")
    return None


def main(
    argv: list[str] | None = None,
    client: httpx.Client | None = None,
    secret: str | None = None,
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(description="Simulate Jira webhook deliveries.")
    parser.add_argument("kind", choices=KINDS, help="which scenario to simulate")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="backend base URL")
    parser.add_argument("--ticket", default="BUG-1042")
    parser.add_argument("--delivery-id", default=None, help="reuse a delivery id (default: a fresh random one)")
    parser.add_argument("--wait", action="store_true", help="after queuing, wait and show the investigation result")
    args = parser.parse_args(argv)

    if secret is None:
        from app.config import Settings  # 延迟导入：只有命令行运行时才读 .env

        secret = Settings().jira_webhook_secret
    if not secret:
        out("JIRA_WEBHOOK_SECRET is not set. Add it to .env (see README) and try again.")
        return 2

    delivery_id = args.delivery_id or f"sim-{uuid.uuid4()}"
    own_client = client is None
    client = client or httpx.Client(timeout=30)
    try:
        return _run(args, client, secret, delivery_id, out)
    except httpx.TransportError as exc:
        # 连不上（后端没启动、端口写错）：给一句人话，而不是一屏 Python 堆栈。
        out(f"Cannot reach the backend at {args.url} ({type(exc).__name__}). Is it running?")
        return 3
    finally:
        if own_client:
            client.close()


def _run(
    args: argparse.Namespace, client: httpx.Client, secret: str, delivery_id: str, out: Callable[[str], None]
) -> int:
    """按场景发送请求并核对状态码。返回退出码：0 全部符合预期，1 有不符的。"""
    unexpected = 0
    for description, body, signature, expected in _plan(args.kind, secret, args.ticket, delivery_id):
        response = send(client, args.url, body, signature, delivery_id)
        verdict = "as expected" if response.status_code == expected else f"UNEXPECTED (wanted {expected})"
        out(f"-> {description}")
        out(f"   <- HTTP {response.status_code} {response.text.strip()}  [{verdict}]")
        if response.status_code != expected:
            unexpected += 1
            continue
        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        if args.wait and data.get("status") == "queued":
            job = wait_for_job(client, args.url, data["job_id"], out)
            if job and job["result"]:
                top = job["result"]["report"]["hypotheses"][0]
                out(f"    most likely cause ({round(top['confidence'] * 100)}%): {top['root_cause']}")
                out(
                    f"    {job['result']['llm_calls']} model calls, "
                    f"{job['result']['input_tokens']:,} in / {job['result']['output_tokens']:,} out tokens"
                )
            elif job:
                out(f"    failed: {job['last_error']}")
    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main())
