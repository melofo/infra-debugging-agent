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

-- ===========================================================================
-- 写操作的人工审批（Phase 6.6）
-- ===========================================================================
-- Agent 只能"提议"一个写操作（比如往 Jira 工单里回写一条评论），不能执行它。
-- 提议进入 pending，由有 approvals:decide 权限的人看过【确切的内容】后批准，系统才去执行。
-- 状态机：pending -> approved -> executing -> executed / failed；pending -> rejected；pending 过期后不能再批准。
CREATE TABLE IF NOT EXISTS proposed_actions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- ON DELETE CASCADE：任务被删掉时，它的待审批提议没有存在意义，一起删。
    job_id        uuid        NOT NULL REFERENCES investigation_jobs(id) ON DELETE CASCADE,
    kind          text        NOT NULL CHECK (kind IN ('jira_comment')),
    ticket_id     text        NOT NULL,
    body          text        NOT NULL,
    -- 内容哈希：批准的是"这一份确切的内容"。批准时要带上审批人看到的哈希，不一致就拒绝；
    -- 执行前再核对一次库里的内容有没有被改过。否则可能出现"批准时看到 A，实际发出去的是 B"（检查时与使用时不一致，TOCTOU）。
    content_hash  text        NOT NULL,
    status        text        NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'approved', 'rejected', 'executing', 'executed', 'failed')),
    proposed_by   text        NOT NULL DEFAULT 'agent',
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,   -- 过期的提议不能再批准：几天前看起来合理的内容，现在可能已经过时
    decided_by    text,
    decided_at    timestamptz,
    decision_note text,
    executed_at   timestamptz,
    error         text,
    -- 同一个任务对同一种操作只会有一个提议：任务重试、Worker 重跑都不会产生重复提议（幂等）。
    UNIQUE (job_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON proposed_actions (status, created_at DESC);

-- 模拟的 Jira 发件箱：这个项目没有连真实的 Jira，"写评论"就是往这张表里写一行。
-- idempotency_key UNIQUE：同一个提议无论被执行几次，只会产生一条评论（真实的 Jira 回写也要做同样的去重）。
CREATE TABLE IF NOT EXISTS jira_outbox (
    id               bigserial PRIMARY KEY,
    idempotency_key  text        NOT NULL UNIQUE,
    ticket_id        text        NOT NULL,
    body             text        NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- 审计日志：谁、在什么时候、对什么、做了什么。只增不改。
-- 用数据库触发器强制"只能追加"：连我们自己的代码也不能改或删（出了事故，它是最后的依据）。
-- 注意这只防得住应用层；能 DROP TRIGGER 的数据库管理员仍然能绕过。
-- 生产环境应该让应用使用一个只有 INSERT/SELECT 权限的数据库角色，并把日志同步到外部的只读存储。
CREATE TABLE IF NOT EXISTS audit_log (
    id           bigserial PRIMARY KEY,
    at           timestamptz NOT NULL DEFAULT now(),
    actor        text        NOT NULL,
    action       text        NOT NULL,
    target_type  text        NOT NULL,
    target_id    text        NOT NULL,
    details      jsonb       NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log (target_type, target_id);

CREATE OR REPLACE FUNCTION audit_log_is_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER audit_log_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION audit_log_is_append_only();
CREATE OR REPLACE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log FOR EACH STATEMENT EXECUTE FUNCTION audit_log_is_append_only();

-- ===========================================================================
-- 会话持久化（Phase 6.7）
-- ===========================================================================
-- 数据库是会话的唯一真相来源：服务重启、多个实例之间，会话都不会丢、不会各说各话。
CREATE TABLE IF NOT EXISTS chat_sessions (
    id              text PRIMARY KEY,           -- 不可猜测的随机串（见 app/sessions.py）
    owner           text        NOT NULL,       -- API 密钥的名字；只有所有者（和管理员）能访问
    scenario        text        NOT NULL,
    ticket_id       text        NOT NULL,
    -- 创建会话时有没有启用知识库检索。工具集在整个会话期间必须一致（历史里已经有对应的工具调用），
    -- 所以要记下来，恢复会话时按同样的方式重建工具。
    uses_knowledge  boolean     NOT NULL DEFAULT false,
    -- 创建时的 agent 配置（v1/v2）：会话的工具集在整个生命周期里必须不变，恢复时按它重建。
    agent_variant   text        NOT NULL DEFAULT 'v1',
    messages        jsonb       NOT NULL DEFAULT '[]',   -- 发给模型的完整历史（含工具结果和厂商原始数据）
    turns           jsonb       NOT NULL DEFAULT '[]',   -- 给界面看的精简记录
    report          jsonb,
    -- 乐观锁版本号：每次保存 +1，保存时要求版本号没变。防止两个实例基于同一份旧历史各写各的，后写的把先写的覆盖掉。
    version         integer     NOT NULL DEFAULT 0,
    -- 租约："这个会话正在处理一条消息，直到 busy_until"。取代了进程内的锁，所以多个实例之间也能互斥；
    -- 持有者崩溃了，租约到期后会话会自动恢复可用，不会被永久锁死。
    -- busy_token 标识租约的持有者：保存和释放都要出示它，租约过期被别人接手后，原来的持有者就不能再写了（围栏）。
    busy_until      timestamptz,
    busy_token      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
-- 给【已经存在的】旧表补上这一列（CREATE TABLE IF NOT EXISTS 不会改已有的表）。
-- 旧会话没有记录配置，默认 'v1'：它们创建时确实是 v1 的工具集。
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS agent_variant text NOT NULL DEFAULT 'v1';
CREATE INDEX IF NOT EXISTS idx_chat_sessions_owner ON chat_sessions (owner, updated_at DESC);
