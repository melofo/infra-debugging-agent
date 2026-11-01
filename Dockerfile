# 后端镜像（Web 服务和 Worker 共用同一个镜像，只是启动命令不同）。
#
#   docker build -t infra-agent-backend .
#   docker run --rm -p 8000:8000 --env-file .env infra-agent-backend                    # Web 服务
#   docker run --rm --env-file .env infra-agent-backend python scripts/run_worker.py    # Worker
#
# 【多阶段构建】第一阶段装依赖，第二阶段只拷贝装好的结果。最终镜像里没有 pip 缓存、编译工具、测试工具，
# 体积更小，攻击面也更小（Java 类比：Maven 构建完只把 jar 放进 JRE 镜像，而不是把整个 JDK 和 ~/.m2 都带上）。

FROM python:3.11-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
# 先只拷贝依赖清单再安装：只要依赖没变，Docker 就复用这一层缓存，改代码后重新构建只要几秒（层缓存）。
COPY requirements-prod.txt .
RUN /opt/venv/bin/pip install -r requirements-prod.txt

FROM python:3.11-slim
# 【不用 root 运行】容器里的进程一旦被攻破，root 能做的事远比普通用户多。固定一个非特权的 uid。
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000
WORKDIR /app
# 只拷贝运行需要的目录：没有 tests、.env、.git（.dockerignore 也再挡一次）。
# data/ 里是模拟场景和知识库文档；scripts/ 里有 Worker 入口和密钥管理脚本。
COPY app ./app
COPY data ./data
COPY scripts ./scripts
USER app
EXPOSE 8000

# 健康检查用标准库，不额外装 curl。/health 只表示"进程活着"（存活探针）；
# /ready 还会检查数据库，更适合做"能不能接流量"的就绪探针（Cloud Run 的启动探针用它）。
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8000'), timeout=2).status == 200 else 1)"

# exec：让 uvicorn 成为 PID 1，这样容器停止时发出的 SIGTERM 直接送到它，触发优雅关闭
# （先停 Worker、把没做完的任务放回队列、再关连接池）。不加 exec 的话信号会被 sh 吞掉，
# 容器会等满 10 秒被强制杀死，正在处理的任务就丢了。
# $PORT：Cloud Run 通过环境变量告诉容器该监听哪个端口。
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
