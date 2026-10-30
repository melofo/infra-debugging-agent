-- 知识库的表结构（runbook 和历史事故复盘，用于语义检索）。
--
-- 和 schema.sql 分开放，是有意的：这里依赖 pgvector 扩展。
-- 如果运行环境的 PostgreSQL 没装 pgvector，这个脚本会失败，但任务队列等核心功能不应该因此跟着坏掉。
-- 应用启动时分别执行，这里失败只会让"知识库检索"不可用（见 app/db.py 的 init_knowledge_schema）。

-- 启用 pgvector 扩展：提供 vector 数据类型、距离运算符和向量索引。
-- 注意：创建扩展需要较高的数据库权限。本地 Docker 里的 agent 用户是超级用户，没问题；
-- 部署到云数据库时需要由管理员预先启用，或者给应用账号授权。
CREATE EXTENSION IF NOT EXISTS vector;

-- 文档表：每篇 runbook / 事故复盘一行，存元数据，用于过滤和增量更新。
CREATE TABLE IF NOT EXISTS knowledge_documents (
    doc_id        text PRIMARY KEY,                                  -- 例如 RB-101、INC-2031
    doc_type      text   NOT NULL CHECK (doc_type IN ('runbook', 'incident')),
    title         text   NOT NULL,
    services      text[] NOT NULL DEFAULT '{}',                       -- 适用的服务；空数组 = 适用于所有服务
    tags          text[] NOT NULL DEFAULT '{}',
    -- 入库时文档内容的哈希：没变就不必重新向量化（向量化要调用接口，花钱花时间）。
    content_hash  text   NOT NULL,
    -- 入库时用的向量化器。换了模型，旧向量和新向量不在同一个"空间"里，没法比较，必须重新入库。
    embedder_id   text   NOT NULL,
    -- 入库时的切块参数。切块方式一变，同一篇文档会切出不同的块，旧向量就过时了，需要重新入库。
    chunking      text   NOT NULL DEFAULT '',
    indexed_at    timestamptz NOT NULL DEFAULT now()
);

-- 【最朴素的数据库迁移】CREATE TABLE IF NOT EXISTS 对"已经存在"的表什么都不做，不会补上新加的列。
-- 所以给已有的数据库（比如你本机已经建好表的开发库）加列，要单独写 ALTER TABLE ... ADD COLUMN IF NOT EXISTS。
-- 两条语句都是幂等的：新库有这一列（上面建表时已包含），旧库会被补上。
-- 项目再大一些，应该换成带版本号的迁移工具（Python 里常用 Alembic，Java 里是 Flyway / Liquibase）。
ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS chunking text NOT NULL DEFAULT '';

-- 块表：每个文档切成若干块，每块一个向量。检索时比较的就是这里的向量。
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id           bigserial PRIMARY KEY,
    -- ON DELETE CASCADE：删除文档时，它的所有块自动一起删除，不会留下没有主人的"孤儿"块。
    doc_id       text    NOT NULL REFERENCES knowledge_documents (doc_id) ON DELETE CASCADE,
    chunk_index  integer NOT NULL,
    heading      text,
    content      text    NOT NULL,
    -- vector(768)：768 维的向量。维度写死在列类型里，插入别的维度会被数据库直接拒绝，
    -- 这样不会悄悄存进一批和查询向量维度对不上的垃圾数据。
    embedding    vector(768) NOT NULL,
    UNIQUE (doc_id, chunk_index)
);

-- HNSW 索引：让"找最近的向量"不必和每一行逐个比较。
-- 没有索引时，检索是精确的全表扫描，数据量大了就慢；
-- HNSW 是近似最近邻（ANN）索引：用一点点精度（偶尔漏掉真正最近的）换来很大的速度提升。
-- vector_cosine_ops 表示按余弦距离建索引，查询时必须用同一个运算符（<=>）才能用上这个索引。
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_embedding
    ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);
