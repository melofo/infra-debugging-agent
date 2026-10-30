"""工具层测试：验证工具返回正确数据、校验拦截坏参数、标准答案不会泄露。"""

import json

import pytest

from app.agent.tools import ToolError, build_investigation_tools
from app.scenario import load_scenario


# fixture 相当于 JUnit 的 @BeforeEach 里准备测试对象：
# 测试函数把 fixture 的名字写在参数里，pytest 就会自动调用它并把返回值传进来。
@pytest.fixture
def scenario():
    return load_scenario("bug-1042")


@pytest.fixture
def registry(scenario):
    return build_investigation_tools(scenario)


async def test_scenario_loads_with_ground_truth_separate(scenario):
    assert scenario.ticket.id == "BUG-1042"
    assert "maximumPoolSize" in scenario.ground_truth.keywords


async def test_get_ticket(registry):
    ticket = await registry.execute("get_ticket", {"ticket_id": "BUG-1042"})
    assert ticket["service"] == "checkout-api"


async def test_get_ticket_not_found_raises_tool_error(registry):
    with pytest.raises(ToolError):
        await registry.execute("get_ticket", {"ticket_id": "BUG-9999"})


async def test_search_logs_filters_by_service_level_and_query(registry):
    result = await registry.execute(
        "search_logs", {"service": "checkout-api", "level": "ERROR", "query": "hikari"}
    )
    assert result["total_matches"] == 3
    assert all(e["level"] == "ERROR" for e in result["entries"])
    # 按时间升序，方便模型判断"错误从什么时候开始"。
    timestamps = [e["timestamp"] for e in result["entries"]]
    assert timestamps == sorted(timestamps)


async def test_search_logs_never_returns_other_services(registry):
    result = await registry.execute("search_logs", {"service": "checkout-api"})
    assert all(e["service"] == "checkout-api" for e in result["entries"])


@pytest.mark.parametrize("tool", ["search_logs", "get_recent_deploys"])
async def test_unknown_service_is_an_error_that_lists_valid_services(registry, tool):
    # 拼错服务名不能静默返回空结果，否则模型会误以为"该服务没有数据"。
    with pytest.raises(ToolError) as exc_info:
        await registry.execute(tool, {"service": "payment-service"})
    assert "payments-service" in str(exc_info.value)  # 错误信息里给出了合法名字，模型可以自己改正


async def test_search_logs_limit_is_capped(registry):
    # 护栏：模型想一次拉 100 条会被参数校验拒绝，而不是真的执行。
    with pytest.raises(ToolError):
        await registry.execute("search_logs", {"service": "checkout-api", "limit": 100})


async def test_get_recent_deploys_newest_first(registry):
    deploys = await registry.execute("get_recent_deploys", {"service": "checkout-api"})
    assert [d["version"] for d in deploys] == ["2.14.0", "2.13.2"]


async def test_search_runbooks_ranks_relevant_first(registry):
    runbooks = await registry.execute("search_runbooks", {"query": "connection pool timeout"})
    assert runbooks[0]["id"] == "RB-101"


async def test_unknown_tool_is_rejected(registry):
    # 白名单：模型编造的工具名不会被执行。
    with pytest.raises(ToolError):
        await registry.execute("delete_database", {})


async def test_missing_required_argument_is_rejected(registry):
    with pytest.raises(ToolError):
        await registry.execute("get_ticket", {})


async def test_specs_describe_every_tool(registry):
    names = {spec.name for spec in registry.specs()}
    assert names == {"get_ticket", "search_logs", "get_recent_deploys", "search_runbooks"}
    search_logs = next(s for s in registry.specs() if s.name == "search_logs")
    assert "service" in search_logs.parameters["properties"]


async def test_tool_outputs_never_contain_ground_truth(scenario, registry):
    # 把所有工具的输出拼起来，确认标准答案的原文没有泄露给 Agent。
    outputs = [
        await registry.execute("get_ticket", {"ticket_id": "BUG-1042"}),
        await registry.execute("search_logs", {"service": "checkout-api"}),
        await registry.execute("get_recent_deploys", {"service": "checkout-api"}),
        await registry.execute("search_runbooks", {"query": "connection pool"}),
    ]
    blob = json.dumps(outputs)
    assert scenario.ground_truth.root_cause not in blob
    assert "ground_truth" not in blob
