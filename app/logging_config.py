"""结构化日志（JSON 日志）。

为什么不用 print？生产环境里日志会被 Cloud Logging 之类的系统收集和查询。
每行日志是一个 JSON 对象时，平台可以按字段过滤，例如 "找出 latency_ms > 5000 的请求"。
纯文本日志做不到这一点，只能靠正则硬抠。

Java 类比：Python 标准库的 logging 就像 SLF4J + Logback。
- Logger  = 你在代码里调用的 logger.info(...)
- Handler = Appender（决定日志写到哪里，这里写到 stdout）
- Formatter = Layout/Encoder（决定日志长什么样，这里输出 JSON）
Cloud Run 会自动收集容器 stdout，所以我们只需要往 stdout 打 JSON 就行。
"""

import json
import logging
import sys
from datetime import datetime, timezone

# LogRecord 自带的标准属性。除了这些以外，用户通过 extra={...} 传入的字段才是我们要额外输出的。
_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # logger.info("llm call", extra={"latency_ms": 120}) 里的 extra 字段会成为 record 的属性，
        # 这里把它们捞出来放进 JSON，实现 "结构化" 的关键一步。
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str：遇到 JSON 不认识的类型（如 datetime）时转成字符串，避免日志本身抛异常。
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", stream=None) -> None:
    # stream 默认是 stdout（Cloud Run 收集容器的 stdout）。
    # 但 stdio 方式的 MCP 服务器必须把日志写到 stderr：stdout 是协议通道，里面多出一行日志，对方就解析失败了。
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    # 直接替换 handlers 而不是 addHandler：uvicorn --reload 会多次导入我们的代码，
    # 如果每次都追加，同一条日志会被打印多遍。
    root.handlers = [handler]
    root.setLevel(level.upper())

    # 第三方库的日志降噪：httpx 每个 HTTP 请求都打一行 INFO（含完整 URL），噪音大；
    # 万一哪天某个 API 把密钥放在 URL 里，它还会把密钥写进日志。统一只保留 WARNING 以上。
    logging.getLogger("httpx").setLevel(logging.WARNING)
