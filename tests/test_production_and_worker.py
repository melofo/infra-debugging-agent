"""生产环境配置的保护，以及独立 Worker 进程的测试。"""

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import jobs
from app.config import Settings

WORKER = Path(__file__).resolve().parents[1] / "scripts" / "run_worker.py"
# conftest 为了不影响其他测试，在环境变量里设了 AUTH_REQUIRED=false，所以这里把"安全的值"都显式写出来。
GOOD = dict(environment="production", auth_required=True, session_store="postgres", cors_origins=["https://ui.example.com"])


def make(**overrides):
    return Settings(_env_file=None, **{**GOOD, **overrides})


def test_a_correct_production_configuration_is_accepted():
    settings = make()
    assert settings.environment == "production" and settings.auth_required and settings.session_store == "postgres"


@pytest.mark.parametrize(
    "overrides,message",
    [
        (dict(auth_required=False), "AUTH_REQUIRED"),
        (dict(session_store="memory"), "SESSION_STORE"),
        (dict(cors_origins=["http://localhost:3000"]), "CORS_ORIGINS"),
        (dict(cors_origins=["https://ui.example.com", "http://127.0.0.1:3000"]), "CORS_ORIGINS"),
    ],
)
def test_unsafe_defaults_are_refused_in_production(overrides, message):
    with pytest.raises(ValidationError, match=message):
        make(**overrides)


def test_all_the_problems_are_reported_together_so_they_can_be_fixed_in_one_go():
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, environment="production", auth_required=False, session_store="memory", cors_origins=["http://localhost:3000"])
    text = str(error.value)
    assert "AUTH_REQUIRED" in text and "SESSION_STORE" in text and "CORS_ORIGINS" in text


def test_local_and_test_environments_keep_the_development_defaults():
    for environment in ("local", "test", "staging"):
        settings = Settings(_env_file=None, environment=environment, auth_required=False, session_store="memory")
        assert settings.auth_required is False


# ============================================================ 独立的 Worker 进程
def worker_env(url: str, **extra):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "GEMINI_API_KEY", "GEMINI_MODEL")}
    env.update(DATABASE_URL=url, WORKER_ENABLED="false", WORKER_POLL_INTERVAL_SECONDS="0.2", LOG_LEVEL="INFO", **extra)
    return env


def test_the_worker_refuses_to_start_without_a_model_and_does_not_traceback(tmp_path):
    result = subprocess.run([sys.executable, str(WORKER)], cwd=tmp_path, env=worker_env("postgresql://x@127.0.0.1:1/x"), capture_output=True, text=True, timeout=30)
    assert result.returncode == 2 and "GEMINI_API_KEY" in result.stderr and "Traceback" not in result.stderr


async def test_a_separate_worker_process_picks_up_jobs_and_stops_cleanly_on_sigterm(pool, tmp_path):
    # 一个"数据坏了"的任务：不需要调用模型就会被永久失败，所以不花钱，也能证明独立进程确实在处理队列
    await jobs.enqueue(pool, "evt-proc-1", "BUG-9999", "does-not-exist")
    process = subprocess.Popen(
        [sys.executable, str(WORKER)], cwd=tmp_path, env=worker_env(pool.conninfo, GEMINI_API_KEY="fake-key-for-test", GEMINI_MODEL="fake-model"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 20
        job = None
        while time.monotonic() < deadline:
            job = (await jobs.list_jobs(pool))[0]
            if job.status == "failed":
                break
            assert process.poll() is None, process.stderr.read()
            await asyncio.sleep(0.3)
        assert job is not None and job.status == "failed" and "invalid job" in job.last_error  # 它真的处理了这个任务

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=15) == 0  # 收到停止信号后优雅退出，退出码 0
    finally:
        if process.poll() is None:
            process.kill()
        process.stdout.close()
        process.stderr.close()
