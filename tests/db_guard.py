"""数据库不可用时：本地"跳过"，CI 里"失败"。

本地没启动数据库时，依赖数据库的测试自动跳过，这样没装 Docker 的人也能跑其他测试。
但在 CI 里，"跳过"会掩盖问题：数据库服务没起来，几十个测试全被跳过，流水线却是绿色的，
大家以为全部通过了。所以 CI 设置 REQUIRE_DB=1，把这类"跳过"变成明确的失败。
"""

import os

import pytest


def unavailable(reason: str):
    if os.environ.get("REQUIRE_DB") == "1":
        pytest.fail(f"REQUIRE_DB=1 but the database is not usable: {reason}", pytrace=False)
    pytest.skip(reason)
