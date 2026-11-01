"""进程存活的"心跳"：一个后台任务定期更新文件的修改时间，健康检查看这个文件够不够新。

【为什么 Worker 需要它】
Web 服务有端口，健康检查访问 /health 就知道它活着。Worker 不监听任何端口，容器编排系统（Docker、Kubernetes、Cloud Run 的工作进程）
没有办法知道它是在正常工作，还是已经卡死：进程还在，但事件循环被堵住，永远不再领任务。没有健康检查，卡死的 Worker 会一直"在线"。

【心跳证明什么、不证明什么】
心跳任务和 Worker 的主循环是两个独立的协程，共用同一个事件循环：
  - 事件循环正常运转（包括 Worker 正在处理一个耗时几十秒的调查）-> 心跳照常更新，健康检查通过；
  - 事件循环被堵死（死锁、同步代码卡住）-> 心跳停止更新，健康检查在几十秒后失败，编排系统可以重启它。
它不证明"任务有进展"（那是另一种监控：队列长度、任务耗时），只证明"进程还有反应"，这正是存活检查该回答的问题。

用文件而不是端口：不需要在 Worker 里再开一个 HTTP 服务（多一个攻击面），健康检查只要读一个文件的时间。
"""

import asyncio
import os
import time
from pathlib import Path

DEFAULT_PATH = "/tmp/worker-heartbeat"
DEFAULT_INTERVAL_SECONDS = 5.0
DEFAULT_MAX_AGE_SECONDS = 30.0  # 要明显大于更新间隔，容忍偶尔的调度延迟，但又不能大到让卡死拖很久才被发现


def heartbeat_path() -> Path:
    return Path(os.environ.get("WORKER_HEARTBEAT_FILE", DEFAULT_PATH))


def beat(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()  # 更新修改时间；文件内容不重要
    os.utime(path, None)


async def run_heartbeat(path: Path, stop: asyncio.Event, interval: float = DEFAULT_INTERVAL_SECONDS) -> None:
    """一直更新心跳，直到 stop 被设置。任何一次写失败都不能让 Worker 崩溃（心跳是辅助，不是业务）。"""
    while not stop.is_set():
        try:
            beat(path)
        except OSError:
            pass  # 写不了（磁盘满、权限）：不更新就是了，健康检查会如实反映"没有心跳"
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except TimeoutError:
            pass


def is_alive(path: str | Path | None = None, max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS) -> bool:
    """健康检查调用它：心跳文件存在，并且最近更新过。不传路径就用和 Worker 相同的默认位置（可被环境变量覆盖）。"""
    try:
        return time.time() - os.path.getmtime(path or heartbeat_path()) < max_age_seconds
    except OSError:
        return False  # 文件不存在（还没启动好，或者从没成功过）= 不健康
