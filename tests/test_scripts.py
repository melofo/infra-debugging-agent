"""把 scripts/ 里的脚本当作【真正的命令行程序】来运行的测试。

为什么需要它？前面的单元测试是在 pytest 里 import 脚本，这时 Python 的模块搜索路径已经被
pytest.ini 设好了，所以即使脚本本身找不到 `app` 包也测不出来。
但用户是直接运行 `python scripts/xxx.py` 的，那时搜索路径完全不同。
这个测试用子进程，在一个无关的目录下，并且不设置 PYTHONPATH，完全复现用户的运行方式。
（我之前正是因为手动验证时设置了 PYTHONPATH，才没发现脚本在用户机器上会报 ModuleNotFoundError。）
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def run_script(name: str, args: list[str], cwd: Path, **env_overrides: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "JIRA_WEBHOOK_SECRET")}
    env.update(env_overrides)
    # cwd 设成一个空的临时目录：既不是项目根目录（模拟"从别处运行"），也没有 .env（不会读到真实密钥）。
    return subprocess.run(
        [sys.executable, str(SCRIPTS / name), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_check_env_runs_without_pythonpath_and_never_prints_secret_values(tmp_path):
    result = run_script(
        "check_env.py",
        [],
        tmp_path,
        GEMINI_API_KEY="FAKE-gemini-key-for-testing-0000",
        GEMINI_MODEL="some-model",
        JIRA_WEBHOOK_SECRET="x" * 43,
        DATABASE_URL="postgresql://agent:HUNTER2-password@127.0.0.1:5432/agent",
    )

    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "JIRA_WEBHOOK_SECRET: set, length 43 -> OK" in result.stdout
    assert "GEMINI_MODEL: some-model" in result.stdout
    for secret in ("FAKE-gemini-key", "HUNTER2", "x" * 43):
        assert secret not in result.stdout + result.stderr


def test_simulator_runs_without_pythonpath_and_reports_an_unreachable_backend_politely(tmp_path):
    # 端口 1 上不会有任何服务。要的就是脚本能正常启动、导入成功、给出一句人话，而不是 Python 堆栈。
    result = run_script(
        "simulate_jira.py", ["created", "--url", "http://127.0.0.1:1"], tmp_path, JIRA_WEBHOOK_SECRET="s" * 43
    )

    assert result.returncode == 3
    assert "Cannot reach the backend" in result.stdout
    assert "Traceback" not in result.stderr


def test_simulator_without_a_secret_explains_what_to_do(tmp_path):
    result = run_script("simulate_jira.py", ["created"], tmp_path)
    assert result.returncode == 2
    assert "JIRA_WEBHOOK_SECRET is not set" in result.stdout


@pytest.mark.parametrize("script", ["check_env.py", "simulate_jira.py"])
def test_scripts_import_cleanly_from_another_directory(script, tmp_path):
    # --help 只做参数解析就退出；check_env 没有参数，直接运行也应成功退出（用假配置）。
    args = ["--help"] if script == "simulate_jira.py" else []
    result = run_script(script, args, tmp_path, JIRA_WEBHOOK_SECRET="s" * 43)
    assert result.returncode == 0, result.stderr
