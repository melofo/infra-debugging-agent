# 前端镜像（Next.js，standalone 输出）。
#
#   docker build -t infra-agent-frontend --build-arg NEXT_PUBLIC_API_URL=http://localhost:8000 frontend
#
# 【NEXT_PUBLIC_API_URL 是构建参数，不是运行参数】
# 带 NEXT_PUBLIC_ 前缀的变量会在构建时被写死进发给浏览器的 JS 里。所以后端地址要在 build 的时候就确定：
# 同一个镜像不能"改个环境变量就指向另一个后端"。这是 Next.js 的设计，不是我们的选择。
# 同样的原因：这里只能放可以公开的东西（后端地址），绝不能放密钥。

FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
# npm ci：严格按 lock 文件安装，保证和开发时一模一样的依赖版本。
RUN npm ci

FROM node:22-slim AS build
WORKDIR /app
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-slim
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
# standalone 输出：一个只含运行所需文件的最小目录（含它自己精简过的 node_modules），不需要完整的依赖树。
# 静态资源和 public 要另外拷贝（Next.js 约定：它们可以交给 CDN，所以不包含在 standalone 里）。
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
# node 镜像自带一个非特权用户 node（uid 1000），用它运行，不用 root。
USER node
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||3000)).then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "server.js"]
