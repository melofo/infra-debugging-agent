"""应用配置。

Java 类比：这相当于 Spring Boot 的 application.yml + @ConfigurationProperties。
pydantic-settings 会自动从【环境变量】和【.env 文件】读取值，并按类型转换和校验。

为什么用环境变量？这是 "12-factor app" 的标准做法：同一份代码，
本地、测试、Cloud Run 只靠不同的环境变量区分，密钥（API key）永远不写进代码或 git。
"""

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def ensure_safe_mcp_endpoint(url: str) -> None:
    """带着 API 密钥访问远程 MCP 服务端时，必须走 https（本机回环地址除外）。

    Bearer 密钥在明文 http 上等于在网线上广播：路径上任何一个设备都能抄走它。
    宁可启动失败，也不要悄悄地把密钥发出去。
    """
    parts = urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS):
        return
    raise ValueError("MCP_HTTP_URL must use https (plain http is only allowed for localhost) because it carries an API key")


class Settings(BaseSettings):
    # 字段名小写，对应环境变量的大写形式：app_name <-> APP_NAME。
    # 等号右边是默认值；环境变量或 .env 里有同名值就会覆盖它。
    app_name: str = "infra-debugging-agent"
    environment: str = "local"
    log_level: str = "INFO"

    # 默认空字符串表示"没配置"。我们不在启动时崩溃，而是在真正调用 LLM 时返回 503，
    # 这样没有 key 的人也能跑 /health 和测试。
    gemini_api_key: str = ""
    gemini_model: str = ""
    llm_timeout_seconds: float = 30.0
    llm_max_attempts: int = 4  # 遇到 429/503 时的最大尝试次数（含第一次）；设为 1 表示不重试
    # Agent 最多调用模型多少轮。硬上限，防止死循环烧钱。
    agent_max_steps: int = 8
    # 数据库连接串。格式：postgresql://用户:密码@主机:端口/库名
    # 默认值对应 docker-compose.yml 里的本地开发数据库（只绑定在本机回环地址，没有真实数据）。
    # 部署到云上时通过环境变量 DATABASE_URL 覆盖，密码来自 Secret Manager，绝不写进代码。
    database_url: str = "postgresql://agent:devpassword@127.0.0.1:5432/agent"
    db_pool_max_size: int = 5
    db_pool_timeout_seconds: float = 5.0  # 借不到连接最多等多久；数据库挂了时让请求尽快失败
    # Jira Webhook 的共享密钥（在 Jira 里配置 webhook 时填的 secret，两边必须一致）。
    # 默认为空 = 未配置。此时 webhook 接口会【拒绝所有请求】（见 api/webhooks.py），
    # 而不是"没密钥就不验证"：宁可不可用，也不能对外开放一个无需认证的入口。
    jira_webhook_secret: str = ""
    webhook_max_body_bytes: int = 262144  # 256KB。Jira 的事件很小，更大的请求一律拒绝
    # 知识库向量化。gemini = 真实的语义向量（需要 GEMINI_API_KEY，花额度）；
    # hash = 离线的特征哈希向量（不花钱、可重复，但只反映用词重叠，只适合测试和没有 key 时的本地开发）。
    embedding_provider: Literal["gemini", "hash"] = "gemini"
    embedding_model: str = "gemini-embedding-001"  # 以你账号里实际可用的向量化模型为准
    # 工具从哪里来：direct = 直接调用 Python 函数；mcp-memory = 同一进程内通过 MCP 协议；
    # mcp-stdio = 启动一个 MCP 服务端子进程。注意：这是【服务端配置】，不允许由请求决定，
    # 否则任何调用方都能让我们为每个请求启动一个子进程（资源耗尽攻击）。
    tool_source: Literal["direct", "mcp-memory", "mcp-stdio", "mcp-http"] = "direct"
    mcp_call_timeout_seconds: float = 20.0
    # tool_source=mcp-http 时：远程 MCP 服务端的地址，和访问它用的 API 密钥（用 agent 专属、最小权限的 key）。
    # SecretStr：打印配置、日志、报错时只显示 **********，不会把密钥带出去。
    mcp_http_url: str = "http://127.0.0.1:8765/mcp"
    mcp_api_key: SecretStr = SecretStr("")
    # 后台 Worker：自动领取并执行排队的调查任务（Jira Webhook 触发的）。
    worker_enabled: bool = True
    worker_poll_interval_seconds: float = 2.0  # 队列为空时，隔多久再看一次
    job_timeout_seconds: float = 120.0  # 一次调查最多允许多久，超时算失败
    # 租约：领走任务后多久没结果就认为 worker 丢失并回收。【必须明显大于】job_timeout_seconds，
    # 否则正在正常工作的任务会被误回收，造成重复执行。
    job_lease_seconds: float = 300.0
    reclaim_interval_seconds: float = 30.0  # 隔多久检查一次"卡死的任务"
    shutdown_grace_seconds: float = 10.0  # 关闭服务时，最多等当前任务多久，超时就放回队列
    job_retry_base_delay_seconds: float = 10.0  # 失败重试的退避起点，之后翻倍
    job_retry_max_delay_seconds: float = 300.0
    # 接口认证。默认【必须认证】（fail closed）：忘了配置不能变成"对全世界开放"。
    # 只有在本地开发、或自动化测试里才应该显式关闭；关闭时启动会打出醒目的警告。
    auth_required: bool = True
    # 允许哪些网页来源调用我们的 API（CORS，见 main.py）。默认只放行本地前端开发服务器。
    # 环境变量里用 JSON 数组写：CORS_ORIGINS='["https://my-ui.example.com"]'
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    # 会话的资源上限。会话存在服务器内存里，没有上限的话，有人不停创建会话就能把内存耗尽（简单的 DoS）。
    max_sessions: int = 100
    max_turns_per_session: int = 20  # 同时也限制了历史（以及 token 成本）的增长

    # env_file：本地开发从项目根目录的 .env 读取。
    # extra="ignore"：.env 里有我们没声明的变量时不报错。
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def _check_mcp_http(self) -> "Settings":
        # 启动时就检查（fail fast），而不是等第一个请求来了才发现配错了。
        if self.tool_source == "mcp-http":
            if not self.mcp_api_key.get_secret_value():
                raise ValueError("TOOL_SOURCE=mcp-http requires MCP_API_KEY")
            ensure_safe_mcp_endpoint(self.mcp_http_url)
        return self


@lru_cache
def get_settings() -> Settings:
    # @lru_cache 让这个函数只执行一次，之后返回同一个对象，相当于 Java 里的单例（singleton）。
    # 好处：.env 只解析一次；测试里也可以用 get_settings.cache_clear() 重置。
    return Settings()
