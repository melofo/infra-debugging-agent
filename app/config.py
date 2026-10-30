"""应用配置。

Java 类比：这相当于 Spring Boot 的 application.yml + @ConfigurationProperties。
pydantic-settings 会自动从【环境变量】和【.env 文件】读取值，并按类型转换和校验。

为什么用环境变量？这是 "12-factor app" 的标准做法：同一份代码，
本地、测试、Cloud Run 只靠不同的环境变量区分，密钥（API key）永远不写进代码或 git。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # 允许哪些网页来源调用我们的 API（CORS，见 main.py）。默认只放行本地前端开发服务器。
    # 环境变量里用 JSON 数组写：CORS_ORIGINS='["https://my-ui.example.com"]'
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    # 会话的资源上限。会话存在服务器内存里，没有上限的话，有人不停创建会话就能把内存耗尽（简单的 DoS）。
    max_sessions: int = 100
    max_turns_per_session: int = 20  # 同时也限制了历史（以及 token 成本）的增长

    # env_file：本地开发从项目根目录的 .env 读取。
    # extra="ignore"：.env 里有我们没声明的变量时不报错。
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    # @lru_cache 让这个函数只执行一次，之后返回同一个对象，相当于 Java 里的单例（singleton）。
    # 好处：.env 只解析一次；测试里也可以用 get_settings.cache_clear() 重置。
    return Settings()
