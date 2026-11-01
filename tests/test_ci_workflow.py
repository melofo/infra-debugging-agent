"""CI 工作流的静态检查：GitHub Actions 没法在本机运行，所以把"不能悄悄变坏的性质"固定下来。

（工作流里的每条命令我都在本机按同样的顺序跑过：pytest、前端的 lint/tsc/test/build、冒烟测试、评估参照线。
这里防的是以后有人改工作流时把安全设置或一致性改掉。）
"""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load(TEXT)
JOBS = WORKFLOW["jobs"]
# YAML 里的 "on" 会被解析成布尔值 True，所以用这个办法取
TRIGGERS = WORKFLOW.get("on", WORKFLOW.get(True))


def steps(job: str) -> list[dict]:
    return JOBS[job]["steps"]


def commands(job: str) -> str:
    return "\n".join(step.get("run", "") for step in steps(job))


def test_the_workflow_has_least_privilege_and_needs_no_secrets():
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in TRIGGERS  # 它会让外部 PR 的代码带着仓库的写权限运行
    assert "pull_request" in TRIGGERS and "push" in TRIGGERS
    assert "secrets." not in TEXT  # 这个流水线不需要任何密钥：测试用假模型，冒烟测试用假密钥


def test_every_action_comes_from_the_official_org_with_a_version_tag():
    used = [step["uses"] for job in JOBS.values() for step in job["steps"] if "uses" in step]
    assert used and all(re.fullmatch(r"actions/[a-z-]+@v\d+", u) for u in used), used


def test_all_jobs_have_timeouts_and_new_commits_cancel_old_runs():
    assert all("timeout-minutes" in job for job in JOBS.values())
    assert WORKFLOW["concurrency"]["cancel-in-progress"] is True
    assert set(JOBS) == {"backend", "frontend", "docker", "audit"}


def test_a_missing_database_fails_the_backend_job_instead_of_skipping_tests():
    assert JOBS["backend"]["env"]["REQUIRE_DB"] == "1"
    assert "pytest" in commands("backend")
    postgres = JOBS["backend"]["services"]["postgres"]
    assert "pg_isready" in postgres["options"] and postgres["env"]["POSTGRES_USER"] == "agent"


def test_the_ci_database_is_the_same_image_as_local_development():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert JOBS["backend"]["services"]["postgres"]["image"] == compose["services"]["db"]["image"]


def test_runtime_versions_match_the_docker_images():
    python_setup = next(s for s in steps("backend") if str(s.get("uses", "")).startswith("actions/setup-python"))
    node_setup = next(s for s in steps("frontend") if str(s.get("uses", "")).startswith("actions/setup-node"))
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    frontend_dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    assert f"python:{python_setup['with']['python-version']}-slim" in dockerfile
    assert f"node:{node_setup['with']['node-version']}-slim" in frontend_dockerfile


def test_the_frontend_job_runs_every_check_in_order():
    commands_ = [s["run"] for s in steps("frontend") if "run" in s]
    assert commands_ == ["npm ci", "npm run lint", "npm run typecheck", "npm test", "npm run build"]
    scripts = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))["scripts"]
    # 类型检查必须先生成 Next 的路由类型，否则在干净的检出里会报"找不到 PageProps/LayoutProps"
    assert scripts["typecheck"].startswith("next typegen") and "tsc --noEmit" in scripts["typecheck"]
    assert JOBS["frontend"]["defaults"]["run"]["working-directory"] == "frontend"


def test_the_docker_job_runs_the_same_smoke_test_used_locally():
    assert "scripts/smoke_test_stack.py" in commands("docker")
    assert (ROOT / "scripts" / "smoke_test_stack.py").is_file()


def test_the_dependency_audit_never_blocks_the_pipeline():
    assert JOBS["audit"]["continue-on-error"] is True
    assert all("continue-on-error" not in JOBS[name] for name in ("backend", "frontend", "docker"))
    assert "pip-audit" in commands("audit") and "npm audit" in commands("audit")


def run_db_tests_without_a_database(require_db: bool):
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "REQUIRE_DB"}
    env["DATABASE_URL"] = "postgresql://agent:x@127.0.0.1:1/agent"  # 一个不会有数据库监听的端口
    if require_db:
        env["REQUIRE_DB"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_jobs.py", "-q", "-p", "no:cacheprovider", "-x"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )


def test_without_a_database_db_tests_skip_locally_but_fail_in_ci_mode():
    local = run_db_tests_without_a_database(require_db=False)
    assert local.returncode == 0 and "skipped" in local.stdout  # 本地：没装 Docker 的人也能跑其他测试
    ci = run_db_tests_without_a_database(require_db=True)
    assert ci.returncode != 0 and "REQUIRE_DB=1" in ci.stdout  # CI：数据库没起来就必须报错，不能悄悄变绿


def test_runners_are_pinned_so_a_platform_migration_cannot_turn_a_green_pipeline_red():
    runners = {job["runs-on"] for job in JOBS.values()}
    assert runners == {"ubuntu-24.04"}  # 不用 ubuntu-latest：GitHub 会把它迁移到新系统版本
    assert "ubuntu-latest" not in TEXT.replace("ubuntu-latest：", "").replace("而不是 ubuntu-latest", "")
