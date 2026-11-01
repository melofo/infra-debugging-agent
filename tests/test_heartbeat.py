"""Worker 的心跳和容器健康检查的测试。

最重要的一个：事件循环被堵死时，心跳必须停止更新（这正是健康检查要发现的"卡死"）；
而 Worker 正在处理耗时的任务（异步等待）时，心跳不能停（否则正常工作的 Worker 会被误判为卡死、被反复重启）。
"""

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml

from app import heartbeat, jobs
from app.heartbeat import beat, is_alive, run_heartbeat

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "scripts" / "run_worker.py"


def age(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


# ============================================================ is_alive
def test_a_missing_heartbeat_is_not_alive(tmp_path):
    assert is_alive(tmp_path / "nope") is False


def test_a_fresh_heartbeat_is_alive_and_a_stale_one_is_not(tmp_path):
    path = tmp_path / "hb"
    beat(path)
    assert is_alive(path, max_age_seconds=30) is True
    age(path, 120)
    assert is_alive(path, max_age_seconds=30) is False
    assert is_alive(path, max_age_seconds=300) is True  # 阈值是可配置的


def test_beat_creates_missing_directories_and_refreshes_an_old_file(tmp_path):
    path = tmp_path / "deep" / "er" / "hb"
    beat(path)
    assert path.exists()
    age(path, 100)
    beat(path)
    assert is_alive(path, 5)


def test_the_default_path_follows_the_environment_variable(tmp_path, monkeypatch):
    custom = tmp_path / "custom-hb"
    monkeypatch.setenv("WORKER_HEARTBEAT_FILE", str(custom))
    assert heartbeat.heartbeat_path() == custom
    assert is_alive() is False
    beat(heartbeat.heartbeat_path())
    assert is_alive() is True  # 不传路径：健康检查和 Worker 读写的是同一个位置


# ============================================================ 后台任务
async def test_the_heartbeat_keeps_refreshing_and_stops_promptly_on_request(tmp_path):
    path, stop = tmp_path / "hb", asyncio.Event()
    task = asyncio.create_task(run_heartbeat(path, stop, interval=0.05))
    await asyncio.sleep(0.15)
    age(path, 100)  # 把它改旧；只要任务还在跑，很快就会被刷新回来
    await asyncio.sleep(0.2)
    assert is_alive(path, 5) is True
    started = time.monotonic()
    stop.set()
    await asyncio.wait_for(task, 2)
    assert time.monotonic() - started < 1  # 收到停止信号后马上结束，不用等满一个间隔


async def test_a_failing_write_never_crashes_the_heartbeat_task(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    stop = asyncio.Event()
    task = asyncio.create_task(run_heartbeat(blocker / "cannot" / "hb", stop, interval=0.05))  # 父路径是个文件：写不进去
    await asyncio.sleep(0.2)
    assert not task.done()  # 辅助功能出错，不能拖垮业务
    stop.set()
    await asyncio.wait_for(task, 2)


def test_a_blocked_event_loop_stops_the_heartbeat_so_the_healthcheck_fails(tmp_path):
    """用一个独立进程模拟"卡死"：事件循环先正常运转，然后被同步代码堵住。"""
    path = tmp_path / "hb"
    code = textwrap.dedent(f"""
        import asyncio, time
        from pathlib import Path
        from app.heartbeat import run_heartbeat
        async def main():
            stop = asyncio.Event()
            asyncio.create_task(run_heartbeat(Path({str(path)!r}), stop, interval=0.1))
            await asyncio.sleep(0.5)      # 正常运转
            time.sleep(2.5)               # 同步代码把整个事件循环堵死（心跳任务也就没机会运行）
        asyncio.run(main())
    """)
    process = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)})
    try:
        time.sleep(0.9)
        assert is_alive(path, max_age_seconds=1.0)  # 还没卡死时是健康的
        time.sleep(1.6)  # 现在已经被堵了约 2 秒
        assert is_alive(path, max_age_seconds=1.0) is False  # 心跳停了：健康检查会发现
    finally:
        process.wait(timeout=10)


async def test_waiting_on_slow_async_work_does_not_stop_the_heartbeat(tmp_path):
    """Worker 处理一个耗时几十秒的调查，本质是在 await 模型和数据库：事件循环是空闲可用的，心跳必须照常更新。"""
    path, stop = tmp_path / "hb", asyncio.Event()
    task = asyncio.create_task(run_heartbeat(path, stop, interval=0.05))
    await asyncio.sleep(0.6)  # 模拟 await 一个很慢的外部调用
    assert is_alive(path, max_age_seconds=0.3)
    stop.set()
    await task


# ============================================================ 真实的 Worker 进程
async def test_the_real_worker_process_beats_while_running_and_stops_beating_after_sigterm(pool, tmp_path):
    path = tmp_path / "worker-hb"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(DATABASE_URL=pool.conninfo, WORKER_ENABLED="false", WORKER_POLL_INTERVAL_SECONDS="0.2",
               GEMINI_API_KEY="fake-key-for-test", GEMINI_MODEL="fake-model", WORKER_HEARTBEAT_FILE=str(path))
    process = subprocess.Popen([sys.executable, str(WORKER)], cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 20
        while not path.exists() and time.monotonic() < deadline:
            assert process.poll() is None, process.stderr.read()
            await asyncio.sleep(0.2)
        assert is_alive(path, 10)  # 启动之后很快就有心跳

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=15) == 0
        frozen = path.stat().st_mtime
        await asyncio.sleep(0.6)
        assert path.stat().st_mtime == frozen  # 进程退出后心跳不再更新
    finally:
        if process.poll() is None:
            process.kill()
        process.stdout.close()
        process.stderr.close()


# ============================================================ compose 里的健康检查
def test_the_worker_container_has_a_real_healthcheck_that_uses_the_heartbeat():
    worker = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["worker"]
    check = worker["healthcheck"]
    assert "disable" not in check  # 没有健康检查，卡死的 Worker 一直"在线"，较新的 Compose 的 --wait 还会直接报错
    assert "app.heartbeat" in " ".join(check["test"]) and check["start_period"]


def test_the_exact_healthcheck_command_passes_with_a_fresh_heartbeat_and_fails_without(tmp_path):
    worker = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["worker"]
    command = worker["healthcheck"]["test"][1:]  # 去掉开头的 "CMD"
    env = {**os.environ, "PYTHONPATH": str(ROOT), "WORKER_HEARTBEAT_FILE": str(tmp_path / "hb")}
    command = [sys.executable if part == "python" else part for part in command]
    assert subprocess.run(command, cwd=ROOT, env=env).returncode == 1  # 没有心跳：不健康
    beat(tmp_path / "hb")
    assert subprocess.run(command, cwd=ROOT, env=env).returncode == 0  # 有新鲜的心跳：健康


def test_the_default_thresholds_are_sane():
    # 阈值要明显大于更新间隔（容忍调度延迟），又不能大到让卡死拖很久才被发现
    assert heartbeat.DEFAULT_MAX_AGE_SECONDS >= 3 * heartbeat.DEFAULT_INTERVAL_SECONDS
    assert heartbeat.DEFAULT_MAX_AGE_SECONDS <= 60
