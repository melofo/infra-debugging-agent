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
