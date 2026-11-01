"""容器整套系统的冒烟测试：构建镜像、起整套容器，逐项验证，最后无论成败都拆掉。

    .venv/bin/python scripts/smoke_test_stack.py              # 本机运行（需要 Docker），CI 里跑的是同一个脚本
    .venv/bin/python scripts/smoke_test_stack.py --keep       # 失败后保留容器方便排查

它把"我手动验证过一遍"变成可以随时重复的检查：以后有人改了 Dockerfile、compose 或启动流程，这里会立刻发现。
检查项：健康/就绪、认证（无 key 401，有 key 200）、CORS、会话在后端重启后还在、独立 Worker 容器处理任务、
前端能访问并带着后端地址、容器不是 root 且镜像里没有 .env、停止容器时优雅退出（退出码 0）。

【安全】用一份临时的 compose 覆盖文件，给后端和 Worker 填入假的模型密钥：
  - 不会把你 .env 里的真实密钥传进测试容器；
  - 整个过程不会调用真实模型（只处理一个"数据坏了"的任务，不需要模型），不花额度；
  - 同时强制 AUTH_REQUIRED=true，不受 .env 里本地开发设置的影响。
用独立的 compose 项目名和空闲端口，所以不会碰你正在运行的开发环境（容器、数据库卷、端口都是分开的）。
"""

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "agentsmoke"
OVERRIDE = """\
services:
  backend:
    environment:
      AUTH_REQUIRED: "true"
      GEMINI_API_KEY: "smoke-test-fake-key"
      GEMINI_MODEL: "smoke-test-fake-model"
  worker:
    environment:
      GEMINI_API_KEY: "smoke-test-fake-key"
      GEMINI_MODEL: "smoke-test-fake-model"
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Smoke:
    def __init__(self, args: argparse.Namespace):
        self.keep = args.keep
        self.failures: list[str] = []
        self.ports = {"DB_PORT": free_port(), "BACKEND_PORT": free_port(), "FRONTEND_PORT": free_port()}
        self.backend = f"http://127.0.0.1:{self.ports['BACKEND_PORT']}"
        self.frontend = f"http://127.0.0.1:{self.ports['FRONTEND_PORT']}"
        self.tmp = tempfile.TemporaryDirectory()
        self.override = Path(self.tmp.name) / "override.yml"
        self.override.write_text(OVERRIDE)

    # ---------------------------------------------------------------- helpers
    def compose(self, *args: str, check=True, timeout=900, input_text=None) -> subprocess.CompletedProcess:
        env = {**__import__("os").environ, **{k: str(v) for k, v in self.ports.items()}}
        return subprocess.run(
            ["docker", "compose", "-p", PROJECT, "-f", str(ROOT / "docker-compose.yml"), "-f", str(self.override), "--profile", "app", *args],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout, check=check, input=input_text,
        )

    def http(self, url: str, method="GET", headers=None, body=None):
        request = urllib.request.Request(url, method=method, headers=headers or {}, data=json.dumps(body).encode() if body is not None else None)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode(), dict(error.headers)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail and not ok else ''}", flush=True)
        if not ok:
            self.failures.append(name)

    def wait_for(self, predicate, timeout: float, interval=0.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(interval)
        return None

    # ---------------------------------------------------------------- the checks
    def run(self) -> int:
        print(f"building and starting the stack (project {PROJECT}, ports {self.ports})", flush=True)
        try:
            up = self.compose("up", "--build", "-d", "--wait", "--wait-timeout", "300", check=False)
            self.check("stack builds and becomes healthy", up.returncode == 0, (up.stderr or up.stdout)[-600:])
            if up.returncode == 0:
                self.checks()
        finally:
            if self.failures:
                self.dump_diagnostics()  # 失败时先把现场留下来（拆掉之前），不然 CI 里只剩一句"某项检查失败"，没法排查
            if self.keep and self.failures:
                print(f"--keep: leaving the stack up (docker compose -p {PROJECT} ...)")
            else:
                self.compose("down", "-v", "--remove-orphans", check=False)
            self.tmp.cleanup()
        print(f"\n{'FAILED: ' + ', '.join(self.failures) if self.failures else 'all checks passed'}")
        return 1 if self.failures else 0

    def dump_diagnostics(self) -> None:
        """打印容器状态和每个服务最后的日志。日志里的密钥不会出现：测试用的是假密钥。"""
        print("\n===== diagnostics (container state and recent logs) =====", flush=True)
        ps = self.compose("ps", "-a", "--format", "{{.Service}}: {{.Status}} (exit code {{.ExitCode}})", check=False)
        print(ps.stdout or ps.stderr, flush=True)
        for service in ("db", "backend", "worker", "frontend"):
            logs = self.compose("logs", "--no-color", "--tail", "40", service, check=False)
            print(f"----- {service} (last 40 lines) -----\n{(logs.stdout or logs.stderr).strip()}\n", flush=True)

    def checks(self) -> None:
        status, body, _ = self.http(f"{self.backend}/health")
        self.check("backend /health", status == 200)
        status, body, _ = self.http(f"{self.backend}/ready")
        self.check("backend /ready (database reachable)", status == 200 and "ready" in body, body)

        self.check("API refuses requests without a key (401)", self.http(f"{self.backend}/jobs")[0] == 401)
        created = self.compose("exec", "-T", "backend", "python", "scripts/manage_api_keys.py", "create", "--name", "smoke", "--role", "admin", check=False)
        key = next((w for w in created.stdout.split() if w.startswith("agk_")), "")
        self.check("API key can be created inside the container", bool(key), created.stderr[-300:])
        auth = {"Authorization": f"Bearer {key}"}
        self.check("API accepts the key (200)", self.http(f"{self.backend}/jobs", headers=auth)[0] == 200)

        preflight = self.http(
            f"{self.backend}/sessions", "OPTIONS",
            {"Origin": f"http://localhost:{self.ports['FRONTEND_PORT']}", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"},
        )
        allow = {k.lower(): v for k, v in preflight[2].items()}
        self.check("CORS allows the web UI origin and the Authorization header", preflight[0] == 200 and "authorization" in allow.get("access-control-allow-headers", "").lower())

        status, body, _ = self.http(f"{self.backend}/sessions", "POST", auth, {})
        session_id = json.loads(body).get("id", "") if status == 201 else ""
        self.check("a chat session can be created", bool(session_id))
        self.compose("restart", "backend", check=False)
        up_again = self.wait_for(lambda: self.http(f"{self.backend}/health")[0] == 200 if self._reachable() else False, 60)
        self.check("backend comes back after a restart", bool(up_again))
        self.check("the session survived the restart (stored in Postgres)", self.http(f"{self.backend}/sessions/{session_id}", headers=auth)[0] == 200)

        self.enqueue_unprocessable_job()
        job = self.wait_for(lambda: self.failed_job(auth), 40)
        self.check("the separate worker container processed a job", bool(job), "no job reached a final state")
        self.check("...and recorded a clear reason without calling any model", bool(job) and "invalid job" in job.get("last_error", ""))

        status, body, _ = self.http(self.frontend + "/")
        self.check("frontend serves the page", status == 200)
        bundle = self.compose("exec", "-T", "frontend", "sh", "-c", f"grep -rl 'localhost:{self.ports['BACKEND_PORT']}' /app/.next/static | wc -l", check=False)
        self.check("frontend bundle contains the backend address (build arg)", bundle.stdout.strip() not in ("", "0"))

        who = self.compose("exec", "-T", "backend", "id", "-u", check=False).stdout.strip()
        self.check("backend runs as a non-root user", who not in ("", "0"), f"uid={who}")
        leaked = self.compose("exec", "-T", "backend", "sh", "-c", "ls -a /app | grep -c '^.env' || true", check=False).stdout.strip()
        self.check("no .env file inside the image", leaked in ("", "0"))

        for service in ("worker", "backend"):
            start = time.monotonic()
            self.compose("stop", service, check=False)
            elapsed = time.monotonic() - start
            code = subprocess.run(["docker", "inspect", f"{PROJECT}-{service}-1", "--format", "{{.State.ExitCode}}"], capture_output=True, text=True).stdout.strip()
            self.check(f"{service} shuts down gracefully (exit 0, under 8s)", code == "0" and elapsed < 8, f"exit={code} in {elapsed:.1f}s")

    def _reachable(self) -> bool:
        try:
            self.http(f"{self.backend}/health")
            return True
        except Exception:
            return False

    def enqueue_unprocessable_job(self) -> None:
        # 一个引用了不存在数据的任务：Worker 会直接把它标记为永久失败，不需要调用任何模型。
        code = (
            "import asyncio\nfrom app import jobs\nfrom app.config import Settings\nfrom app.db import create_pool\n"
            "async def main():\n    pool = create_pool(Settings().database_url, max_size=1)\n    await pool.open(wait=True, timeout=10)\n"
            "    await jobs.enqueue(pool, 'smoke-evt', 'BUG-9999', 'does-not-exist')\n    await pool.close()\nasyncio.run(main())\n"
        )
        self.compose("exec", "-T", "backend", "python", "-c", code, check=False)

    def failed_job(self, auth: dict):
        try:
            status, body, _ = self.http(f"{self.backend}/jobs", headers=auth)
            jobs = json.loads(body) if status == 200 else []
        except Exception:
            return None
        return next((j for j in jobs if j["status"] == "failed"), None)


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test the containerised stack.")
    parser.add_argument("--keep", action="store_true", help="leave the containers running when a check fails")
    sys.exit(Smoke(parser.parse_args()).run())


if __name__ == "__main__":
    main()
