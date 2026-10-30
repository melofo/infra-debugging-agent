"""检查 .env 配置是否被应用正确读取。【只输出"有没有 / 多长 / 格式对不对"，绝不输出密钥的值。】

运行：  .venv/bin/python scripts/check_env.py

为什么不直接 cat .env 看？因为屏幕输出、终端录屏、聊天记录里都可能留下密钥。
这个脚本的原则：输出本身必须是可以安全贴给别人看的。
"""

import sys
from pathlib import Path

# 直接运行 `python scripts/xxx.py` 时，Python 的模块搜索路径里是【脚本所在的目录】（scripts/），
# 不是项目根目录，所以 `import app` 会失败。把项目根目录加进去，脚本在任何目录下都能运行。
# （之前我只在设置了 PYTHONPATH 的环境里试过，等于替用户掩盖了这个问题。）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from urllib.parse import urlsplit  # noqa: E402

from app.config import Settings  # noqa: E402


def describe_secret(name: str, value: str, expected_length: int | None = None) -> str:
    if not value:
        return f"{name}: NOT SET"
    problems = []
    if value != value.strip():
        problems.append("has leading/trailing whitespace")
    if any(ch in value for ch in " \t\"'"):
        problems.append("contains a space or quote character (remove quotes in .env)")
    if expected_length and len(value) != expected_length:
        problems.append(f"length is not the expected {expected_length}")
    status = "OK" if not problems else "PROBLEM: " + "; ".join(problems)
    return f"{name}: set, length {len(value)} -> {status}"


def main() -> None:
    settings = Settings()
    print(describe_secret("GEMINI_API_KEY", settings.gemini_api_key))
    # 模型名不是密钥，直接显示出来，方便核对有没有写对。
    print(f"GEMINI_MODEL: {settings.gemini_model or 'NOT SET'}")
    # token_urlsafe(32) 生成的字符串长度是 43。
    print(describe_secret("JIRA_WEBHOOK_SECRET", settings.jira_webhook_secret, expected_length=43))
    # 数据库地址只显示主机、端口、库名，不显示密码。
    db = urlsplit(settings.database_url)
    print(f"DATABASE_URL: host={db.hostname} port={db.port} database={db.path.lstrip('/')} (password hidden)")
    print(f"WORKER_ENABLED: {settings.worker_enabled}")


if __name__ == "__main__":
    main()
