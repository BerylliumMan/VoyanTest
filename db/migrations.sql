-- ============================================================
-- VoyanTest 数据库迁移 SQL
-- 用法: psql -h <host> -p <port> -U <user> -d <db> -f migrations.sql
-- ============================================================

-- 2026-07-15: users 表新增 nickname / email 字段
ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255);

-- 2026-07-14: run_batches 表新增 triggered_by 字段（执行用户）
ALTER TABLE run_batches ADD COLUMN IF NOT EXISTS triggered_by VARCHAR(255);

-- 2026-07-14: recording_sessions 表新增 events_data 字段（录制事件持久化）
ALTER TABLE recording_sessions ADD COLUMN IF NOT EXISTS events_data TEXT;

-- 2026-07-14: test_runs 表 case_id 解除 NOT NULL 约束（删除用例后保留报告记录）
ALTER TABLE test_runs ALTER COLUMN case_id DROP NOT NULL;

-- 2026-07-15: gen_sessions 表新增 user_id 字段（权限隔离）
ALTER TABLE gen_sessions ADD COLUMN IF NOT EXISTS user_id INTEGER;

-- 2026-07-21: ai_configs 表新增 max_context_tokens 字段（上下文窗口大小）
ALTER TABLE ai_configs ADD COLUMN IF NOT EXISTS max_context_tokens INTEGER DEFAULT 131072;

-- 2026-07-26: prompt_templates 表（提示词模板管理）
CREATE TABLE IF NOT EXISTS prompt_templates (
    id              SERIAL PRIMARY KEY,
    key             VARCHAR(100) NOT NULL,
    name            VARCHAR(200) NOT NULL,
    category        VARCHAR(50) NOT NULL,
    content         TEXT NOT NULL,
    variables       JSONB NOT NULL DEFAULT '[]',
    version         INTEGER NOT NULL DEFAULT 1,
    is_active       BOOLEAN NOT NULL DEFAULT false,
    description     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(key, version)
);
