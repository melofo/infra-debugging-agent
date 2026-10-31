"""Agent 的知识库检索工具（search_knowledge）及其接线的测试（需要带 pgvector 的 PostgreSQL）。

用离线的 HashEmbedder 入库，所以验证的是"管道"：工具集的选择、参数校验、错误处理、接线。
语义质量由 Phase 5.6 的评估来衡量，不在这里。
"""

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from app import jobs
from app.agent.common import SUBMIT_TOOL_NAME, SYSTEM_PROMPT, AgentError, submit_spec
from app.agent.loop import InvestigationAgent
from app.agent.sdk_afc import SdkAfcAgent
from app.agent.tools import ToolError, build_investigation_tools
from app.config import Settings
from app.llm.base import LLMResponse
from app.llm.gemini import GeminiClient
from app.main import app
from app.rag.documents import load_documents
from app.rag.embedder import EmbeddingError, HashEmbedder
from app.rag.ingest import ingest
from app.rag.retrieval import KnowledgeBase, KnowledgeIndexMismatch
from app.rag.runtime import active_knowledge_base
from app.scenario import load_scenario
from app.sessions import SessionStore, get_session_store
from app.worker import Worker
from tests.fakes import GET_TICKET, VALID_REPORT_ARGS, ScriptedLLM, ToolCall, calls, final_text


@pytest.fixture
async def kb(knowledge_pool):
    embedder = HashEmbedder()
    await ingest(knowledge_pool, embedder, load_documents())
    return KnowledgeBase(knowledge_pool, embedder)


@pytest.fixture
def scenario():
    return load_scenario("bug-1042")


class StubKnowledge(KnowledgeBase):
    """让检索按指定方式失败或返回空，用来测试工具对各种故障的处理。"""

    def __init__(self, error: Exception | None = None, hits=None):
        self._error, self._hits = error, hits or []

    async def known_services(self):
        return ["checkout-api"]

    async def search(self, *args, **kwargs):
        if self._error:
            raise self._error
        return self._hits


POOL_QUERY = "requests time out waiting for a database connection, connection pool exhausted"


# ------------------------------------------------------------ 工具集的选择
async def test_with_a_knowledge_base_the_semantic_tool_replaces_the_keyword_tool(scenario, kb):
    names = build_investigation_tools(scenario, kb).names()
    assert "search_knowledge" in names and "search_runbooks" not in names


def test_without_a_knowledge_base_the_keyword_tool_is_the_fallback(scenario):
    names = build_investigation_tools(scenario, None).names()
    assert "search_runbooks" in names and "search_knowledge" not in names


# ------------------------------------------------------------ 工具的行为
async def test_results_carry_the_document_id_to_cite(scenario, kb):
    registry = build_investigation_tools(scenario, kb)
    output = await registry.execute("search_knowledge", {"query": POOL_QUERY})

    results = output["results"]
    assert 1 <= len(results) <= 5
    assert {"doc_id", "doc_type", "title", "section", "relevance", "content"} <= set(results[0])
    assert [r["relevance"] for r in results] == sorted((r["relevance"] for r in results), reverse=True)
    assert {r["doc_id"] for r in results[:2]} & {"RB-101", "INC-2031"}


async def test_at_most_two_chunks_per_document_so_results_cover_more_documents(scenario, kb):
    output = await build_investigation_tools(scenario, kb).execute(
        "search_knowledge", {"query": POOL_QUERY, "limit": 8}
    )
    ids = [r["doc_id"] for r in output["results"]]
    assert all(ids.count(doc_id) <= 2 for doc_id in set(ids))


async def test_doc_type_filter(scenario, kb):
    output = await build_investigation_tools(scenario, kb).execute(
        "search_knowledge", {"query": POOL_QUERY, "doc_type": "incident", "limit": 8}
    )
    assert output["results"] and {r["doc_type"] for r in output["results"]} == {"incident"}


async def test_an_unknown_service_is_an_error_that_lists_the_valid_ones(scenario, kb):
    # 和日志、发布工具一样：写错服务名不能静默返回空结果，否则模型会误以为"这个服务没有相关文档"。
    with pytest.raises(ToolError) as error:
        await build_investigation_tools(scenario, kb).execute(
            "search_knowledge", {"query": POOL_QUERY, "service": "checkout-service"}
        )
    assert "search-service" in str(error.value) and "checkout-api" in str(error.value)


@pytest.mark.parametrize(
    "args",
    [
        {"query": "ab"},  # 太短
        {"query": "x" * 501},  # 太长
        {"query": POOL_QUERY, "limit": 20},  # 超过上限
        {"query": POOL_QUERY, "limit": 0},
        {"query": POOL_QUERY, "doc_type": "memo"},  # 不是合法类型
        {},  # 缺少必填参数
    ],
)
async def test_invalid_arguments_are_rejected_before_any_search(scenario, kb, args):
    with pytest.raises(ToolError):
        await build_investigation_tools(scenario, kb).execute("search_knowledge", args)


async def test_no_matches_returns_an_empty_list_with_a_hint(scenario):
    registry = build_investigation_tools(scenario, StubKnowledge(hits=[]))
    output = await registry.execute("search_knowledge", {"query": "something obscure"})
    assert output["results"] == [] and "different wording" in output["note"]


# ------------------------------------------------------------ 故障处理：模型能继续，且不泄露内部细节
@pytest.mark.parametrize(
    "error, expected",
    [
        (EmbeddingError("429 quota exceeded for project secret-project-123"), "temporarily unavailable"),
        (psycopg.OperationalError("connection to 10.0.0.5 refused"), "temporarily unavailable"),
        (KnowledgeIndexMismatch("indexed with gemini:x but queries use hash:768"), "re-indexed"),
    ],
)
async def test_failures_become_safe_tool_errors_without_internal_details(scenario, error, expected):
    registry = build_investigation_tools(scenario, StubKnowledge(error=error))

    with pytest.raises(ToolError) as caught:
        await registry.execute("search_knowledge", {"query": "connection pool problem"})

    message = str(caught.value)
    assert expected in message
    # 工具错误会回传给模型、最终可能显示给用户：不能带出内部地址、项目名、模型名这类信息。
    for secret in ("secret-project-123", "10.0.0.5", "gemini:x", "hash:768", "429"):
        assert secret not in message


# ------------------------------------------------------------ 是否启用知识库
async def test_active_knowledge_base_rules(knowledge_pool, kb):
    assert await active_knowledge_base(None) is None  # 没有配置知识库
    assert await active_knowledge_base(kb) is kb  # 已入库
    # 知识库是用另一个模型建的索引：当前模型下"没有文档"，应降级而不是返回无意义的结果。
    class OtherModel(HashEmbedder):
        @property
        def id(self):
            return "other-model:768"

    assert await active_knowledge_base(KnowledgeBase(knowledge_pool, OtherModel())) is None


async def test_an_empty_knowledge_base_is_not_used(knowledge_pool):
    # 还没运行入库脚本：不应让 Agent 去搜一个空的知识库。
    assert await active_knowledge_base(KnowledgeBase(knowledge_pool, HashEmbedder())) is None


# ------------------------------------------------------------ 提示词与证据来源
def test_the_prompt_tells_the_model_that_past_incidents_are_guidance_not_proof():
    assert "guidance, not proof" in SYSTEM_PROMPT
    assert "INC-2031" in SYSTEM_PROMPT  # 给出了引用文档编号的例子


def test_the_report_schema_accepts_incident_evidence():
    schema = submit_spec().parameters
    sources = schema["$defs"]["EvidenceSource"]["enum"]
    assert "incident" in sources and "runbook" in sources


# ------------------------------------------------------------ 在 Agent 循环里使用
async def test_the_agent_can_search_the_knowledge_base_and_cite_a_past_incident(scenario, kb):
    registry = build_investigation_tools(scenario, kb)
    report_args = {
        "summary": "Pool exhausted after the deploy.",
        "hypotheses": [
            {
                "root_cause": "maximumPoolSize reduced",
                "confidence": 0.9,
                "evidence": [
                    {"source": "git", "summary": "pool 50 -> 5", "reference": "7be41d0"},
                    {"source": "incident", "summary": "Same pattern as a past incident", "reference": "INC-2031"},
                ],
            }
        ],
    }
    llm = ScriptedLLM(
        [
            calls(ToolCall("search_knowledge", {"query": POOL_QUERY})),
            # 证据要引用发布记录里的 commit：先查过 get_recent_deploys，才通过证据落地检查。
            calls(ToolCall("get_recent_deploys", {"service": "checkout-api"})),
            calls(ToolCall(SUBMIT_TOOL_NAME, report_args)),
            final_text(),
        ]
    )

    result = await InvestigationAgent(llm, registry).investigate("BUG-1042")

    assert [s.tool for s in result.steps] == ["search_knowledge", "get_recent_deploys", SUBMIT_TOOL_NAME]
    assert result.steps[0].ok
    assert result.report.hypotheses[0].evidence[1].source == "incident"
    # 第二次调用模型时，检索结果已经在对话历史里了。
    fed_back = llm.seen_messages[1][-1].tool_results[0].result
    assert fed_back["results"] and "doc_id" in fed_back["results"][0]


async def test_a_failed_knowledge_search_does_not_stop_the_investigation(scenario):
    registry = build_investigation_tools(scenario, StubKnowledge(error=EmbeddingError("boom")))
    llm = ScriptedLLM(
        [
            calls(ToolCall("search_knowledge", {"query": "pool problem"})),
            calls(GET_TICKET, ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS)),
            final_text(),
        ]
    )

    result = await InvestigationAgent(llm, registry).investigate("BUG-1042")

    assert result.steps[0].ok is False and result.report is not None  # 工具失败了，调查照常完成


# ------------------------------------------------------------ SDK 版本
class CapturingGemini(GeminiClient):
    def __init__(self):
        self.functions = {}

    async def generate_with_python_tools(self, prompt, functions, system=None, max_remote_calls=8):
        self.functions = {f.__name__: f for f in functions}
        return LLMResponse(text="", input_tokens=1, output_tokens=1, latency_ms=1)


async def test_the_sdk_version_exposes_the_same_knowledge_tool(scenario, kb):
    gemini = CapturingGemini()
    with pytest.raises(AgentError):  # 没提交报告，调查会失败；这里只关心暴露了哪些函数
        await SdkAfcAgent(gemini, build_investigation_tools(scenario, kb)).investigate("BUG-1042")

    assert "search_knowledge" in gemini.functions and "search_runbooks" not in gemini.functions
    output = await gemini.functions["search_knowledge"](query=POOL_QUERY)  # 包装函数能真正查到结果
    assert output["results"]


async def test_the_sdk_version_falls_back_to_the_keyword_tool_without_a_knowledge_base(scenario):
    gemini = CapturingGemini()
    with pytest.raises(AgentError):
        await SdkAfcAgent(gemini, build_investigation_tools(scenario, None)).investigate("BUG-1042")
    assert "search_runbooks" in gemini.functions and "search_knowledge" not in gemini.functions


# ------------------------------------------------------------ Worker
class ToolRecordingLLM:
    """记录模型被提供了哪些工具，然后直接提交报告结束。"""

    def __init__(self):
        self.tool_names: list[str] = []

    async def generate(self, prompt, system=None):
        raise NotImplementedError

    async def generate_with_tools(self, messages, tools, system=None):
        self.tool_names = [t.name for t in tools]
        last = messages[-1]
        if last.role == "user":
            return calls(GET_TICKET, ToolCall(SUBMIT_TOOL_NAME, VALID_REPORT_ARGS))
        return final_text()


async def run_one_job(pool, knowledge) -> list[str]:
    await jobs.enqueue(pool, "evt-1", "BUG-1042", "bug-1042")
    llm = ToolRecordingLLM()
    await Worker(pool, llm, Settings(_env_file=None), worker_id="w1", knowledge=knowledge).run_once()
    return llm.tool_names


async def test_the_worker_uses_the_knowledge_tool_when_the_knowledge_base_is_ready(knowledge_pool, kb):
    assert "search_knowledge" in await run_one_job(knowledge_pool, kb)


async def test_the_worker_falls_back_when_there_is_no_knowledge_base(knowledge_pool):
    names = await run_one_job(knowledge_pool, None)
    assert "search_runbooks" in names and "search_knowledge" not in names


async def test_the_worker_notices_when_the_knowledge_base_gets_ingested_without_a_restart(knowledge_pool):
    empty = KnowledgeBase(knowledge_pool, HashEmbedder())
    assert "search_runbooks" in await run_one_job(knowledge_pool, empty)  # 还没入库

    await ingest(knowledge_pool, HashEmbedder(), load_documents())  # 之后运行了入库脚本，没有重启服务
    async with knowledge_pool.connection() as conn:
        await conn.execute("TRUNCATE investigation_jobs CASCADE")
    assert "search_knowledge" in await run_one_job(knowledge_pool, empty)  # 下一个任务就用上了


# ------------------------------------------------------------ 会话与启动流程
async def test_new_sessions_get_the_knowledge_tool_when_available(kb):
    store = SessionStore()
    app.dependency_overrides[get_session_store] = lambda: store
    app.state.knowledge = kb
    try:
        response = TestClient(app).post("/sessions", json={})
    finally:
        app.state.knowledge = None
        app.dependency_overrides.clear()
    assert response.status_code == 201
    assert "search_knowledge" in store.peek(response.json()["id"]).registry.names()


def test_new_sessions_use_the_keyword_tool_without_a_knowledge_base():
    store = SessionStore()
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        response = TestClient(app).post("/sessions", json={})
    finally:
        app.dependency_overrides.clear()
    assert "search_runbooks" in store.peek(response.json()["id"]).registry.names()


def lifespan_settings(**overrides) -> Settings:
    base_url = Settings(_env_file=None).database_url
    return Settings(
        _env_file=None,
        worker_enabled=False,
        database_url=make_conninfo(base_url, dbname="agent_test"),
        db_pool_timeout_seconds=2,
        **overrides,
    )


def test_startup_creates_the_knowledge_base_when_the_embedder_is_configured(knowledge_pool, monkeypatch):
    monkeypatch.setattr("app.main.get_settings", lambda: lifespan_settings(embedding_provider="hash"))
    with TestClient(app):
        assert isinstance(app.state.knowledge, KnowledgeBase) and app.state.knowledge_ready is True


def test_startup_without_an_embedder_disables_knowledge_search_but_still_starts(knowledge_pool, monkeypatch):
    monkeypatch.setattr("app.main.get_settings", lambda: lifespan_settings(embedding_provider="gemini", gemini_api_key=""))
    with TestClient(app) as client:
        assert app.state.knowledge is None
        assert client.get("/health").status_code == 200
