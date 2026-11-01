"""容器化配置的静态检查：不需要 Docker，只读文件。

镜像和 compose 文件里最容易"悄悄变坏"的几件事，都在这里固定下来：
  - 容器不能用 root 运行；密钥不能被打进镜像；停止信号要能送到应用进程（否则优雅关闭失效）；
  - 开发用的 `docker compose up -d db` 不能被改成会顺带启动整套应用；
  - 数据库端口只绑定本机；compose 文件里不能出现密钥；
  - 前端的锁文件必须完整（Linux 上 npm ci 会因为缺少可选依赖失败）、并且只指向公共仓库。
（这些性质我也在真实的容器里手动验证过一遍：非 root、无 .env、SIGTERM 在 0.5 秒内退出码 0。这里防的是以后改坏。）
"""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BACKEND = (ROOT / "Dockerfile").read_text(encoding="utf-8")
FRONTEND = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def instructions(dockerfile: str) -> list[str]:
    """去掉注释，把续行合并，返回每条指令。"""
    text = "\n".join(l for l in dockerfile.splitlines() if not l.strip().startswith("#"))
    return [" ".join(block.split()) for block in re.sub(r"\\\n", " ", text).split("\n") if block.strip()]


def test_the_backend_image_runs_as_a_non_root_user_and_copies_only_what_it_needs():
    lines = instructions(BACKEND)
    users = [l for l in lines if l.startswith("USER ")]
    assert users and users[-1] != "USER root" and users[-1] == "USER app"
    copies = [l for l in lines if l.startswith("COPY ")]
    assert "COPY . ." not in copies and "COPY . /app" not in copies  # 不能把整个项目目录（含 .env、.git）拷进去
    assert {"COPY app ./app", "COPY data ./data", "COPY scripts ./scripts"} <= set(copies)
    assert not any(".env" in l for l in copies)


def test_the_backend_installs_only_the_production_requirements():
    assert "requirements-prod.txt" in BACKEND and "pip install -r requirements.txt" not in BACKEND


def test_the_stop_signal_reaches_the_application_so_graceful_shutdown_works():
    cmd = next(l for l in instructions(BACKEND) if l.startswith("CMD "))
    assert "exec uvicorn" in cmd  # 没有 exec，SIGTERM 会被 sh 吞掉，容器要等满超时被强制杀死，正在处理的任务就丢了
    assert "${PORT" in cmd  # Cloud Run 通过 PORT 环境变量指定监听端口
    assert any(l.startswith("HEALTHCHECK") for l in instructions(BACKEND))


def test_secrets_cannot_enter_the_image_through_the_build_context():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert {".env", ".env.*", ".git", ".venv", "eval_results", "tests"} <= set(ignored)
    # 知识库文档是 data/ 下的 .md 文件：忽略规则只能针对根目录的 *.md，不能误删它们
    assert "*.md" not in ignored and "/*.md" in ignored
    assert {".env", ".env.*", "node_modules", ".next"} <= set((ROOT / "frontend" / ".dockerignore").read_text(encoding="utf-8").splitlines())


def test_the_frontend_image_is_a_standalone_build_run_by_an_unprivileged_user():
    lines = instructions(FRONTEND)
    assert "USER node" in lines and "RUN npm ci" in lines  # npm ci：严格按锁文件安装
    assert any(l.startswith("CMD") and "server.js" in l for l in lines)
    assert 'output: "standalone"' in (ROOT / "frontend" / "next.config.ts").read_text(encoding="utf-8")
    assert "NEXT_PUBLIC_API_URL" in FRONTEND  # 后端地址是构建参数，不是运行参数


def test_the_frontend_lockfile_is_complete_for_linux_and_uses_only_the_public_registry():
    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    packages = lock["packages"]
    # 在 macOS 上生成的锁文件缺了这两个可选依赖，Linux 上的 npm ci 会直接报错（容器构建就会失败）
    assert "node_modules/@emnapi/core" in packages and "node_modules/@emnapi/runtime" in packages
    resolved = [p["resolved"] for p in packages.values() if "resolved" in p]
    assert resolved and all(url.startswith("https://registry.npmjs.org/") for url in resolved)  # 不能混进公司内部的仓库地址


def test_the_plain_db_command_still_starts_only_the_database():
    services = COMPOSE["services"]
    assert "profiles" not in services["db"]  # `docker compose up -d db` 的日常开发用法
    for name in ("backend", "worker", "frontend"):
        assert services[name]["profiles"] == ["app"], name


def test_the_database_is_only_reachable_from_this_machine_and_no_secret_is_written_in_the_file():
    assert all(p.startswith("127.0.0.1:") for p in COMPOSE["services"]["db"]["ports"])
    for name in ("backend", "frontend"):
        assert all(p.startswith("127.0.0.1:") for p in COMPOSE["services"][name]["ports"]), name
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert not re.search(r"(GEMINI_API_KEY|JIRA_WEBHOOK_SECRET|API_KEY)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}", text)


def test_web_and_worker_are_separate_services_sharing_one_image():
    services = COMPOSE["services"]
    assert services["backend"]["environment"]["WORKER_ENABLED"] == "false"  # Web 服务不在进程内跑 Worker，避免双份
    assert services["worker"]["command"] == ["python", "scripts/run_worker.py"]
    assert services["worker"]["image"] == services["backend"]["image"]
    for name in ("backend", "worker"):
        assert "@db:5432" in services[name]["environment"]["DATABASE_URL"]  # 容器里数据库叫 db，不是 localhost
    assert services["backend"]["depends_on"]["db"]["condition"] == "service_healthy"
