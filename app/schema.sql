-- 数据库表结构。应用启动时执行一次（见 app/db.py 的 init_schema）。
--
-- 每条语句都写成 IF NOT EXISTS，所以重复执行是安全的（幂等）。
-- 这是最简单的"建表"方式，适合现在的阶段。项目变大后应该用专门的迁移工具来管理表结构的演进
-- （Java 世界的 Flyway / Liquibase，Python 里常用 Alembic），每次改动是一个带版本号的迁移脚本。

-- 调查任务表：Jira Webhook 到达后，每个事件对应一行。它同时充当"任务队列"。
-- 为什么用数据库当队列，而不是 Redis、RabbitMQ 或 Kafka？
--   1. 任务本来就要持久化、要查状态，数据库天然满足；
--   2. 少引入一个组件，部署和运维更简单；
--   3. 我们的量级（每分钟几个）远用不到专门的消息队列。
-- 如果以后吞吐量大幅增长，再换成专门的队列，这是常见的演进路径。
CREATE TABLE IF NOT EXISTS investigation_jobs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- 幂等键：Jira 事件的唯一标识。UNIQUE 约束由数据库保证，
    -- 即使两个请求同时到达，也只有一个能插入成功，另一个会得到唯一冲突。
    -- 这比"先查有没有、没有再插入"可靠得多（那种写法在并发下有竞态条件）。
    event_id      text        NOT NULL UNIQUE,

    ticket_id     text        NOT NULL,
    scenario      text        NOT NULL,

    -- 任务状态机：queued(排队) -> running(执行中) -> succeeded(成功) / failed(最终失败)
    -- 失败但还能重试时，会从 running 回到 queued。CHECK 约束保证表里不会出现非法的状态值。
    status        text        NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),

    attempts      integer     NOT NULL DEFAULT 0,   -- 已经尝试了几次
    max_attempts  integer     NOT NULL DEFAULT 3,   -- 最多尝试几次，用完就标记 failed

    -- 最早什么时候可以执行。重试时把它设成"现在 + 退避时间"，这样失败的任务不会立刻又被领走。
    run_after     timestamptz NOT NULL DEFAULT now(),

    -- 谁在什么时候领走了这个任务。worker 如果崩溃，任务会一直停在 running，
    -- 靠 locked_at 判断"领走太久了"，把它回收重新排队。
    locked_at     timestamptz,
    locked_by     text,

    last_error    text,    -- 最近一次失败的原因，排查问题用
    result        jsonb,   -- 调查结果（报告、步骤、token 用量），jsonb 是 PostgreSQL 的二进制 JSON 类型

    -- timestamptz = 带时区的时间戳。永远用它，不要用不带时区的 timestamp（和 Java 里该用 Instant 同理）。
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz
);

-- 部分索引（partial index）：只给 status='queued' 的行建索引。
-- worker 反复执行的查询是"找最早该执行的排队任务"，已完成的历史任务会越来越多，
-- 但它们不在这个索引里，所以查询始终很快，索引也很小。
CREATE INDEX IF NOT EXISTS idx_jobs_claimable ON investigation_jobs (run_after) WHERE status = 'queued';

-- 回收卡死任务时用：找"running 太久"的任务。
CREATE INDEX IF NOT EXISTS idx_jobs_running ON investigation_jobs (locked_at) WHERE status = 'running';

-- API 密钥：调用我们接口的人/程序用它证明身份。
-- 【只存哈希，不存明文】数据库泄露时，攻击者拿到的哈希没法当作钥匙使用。
-- 明文密钥只在创建时显示一次（见 scripts/manage_api_keys.py），之后谁也看不到，包括我们自己。
CREATE TABLE IF NOT EXISTS api_keys (
    key_id        text PRIMARY KEY,                 -- 公开的查找标识，是密钥的一部分（agk_<key_id>.<secret>）
    name          text NOT NULL,                    -- 这把钥匙属于谁/什么用途，出现在日志和审计里
    role          text NOT NULL CHECK (role IN ('viewer', 'operator', 'admin')),
    key_hash      text NOT NULL,                    -- SHA-256(secret)
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz,                      -- 为空表示不过期
    revoked_at    timestamptz,                      -- 吊销后立刻失效，但保留记录供审计
    last_used_at  timestamptz
);

-- 名字用来标识"是谁"（会话的所有者、日志和审计里的操作人），所以同一时刻不能有两把有效的钥匙同名，
-- 否则一个人就能冒充另一个人拿到他的会话。换钥匙时先吊销旧的，新的可以沿用同一个名字。
CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_active_name ON api_keys (name) WHERE revoked_at IS NULL;
