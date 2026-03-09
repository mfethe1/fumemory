-- memU schema extension: Search & Audit Logs (Winnie's Lane)

-- 1. Web Search Cache
CREATE TABLE IF NOT EXISTS web_searches (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    query         TEXT NOT NULL,
    query_hash    VARCHAR(64) NOT NULL, -- fast lookup for exact matches
    summary       TEXT,                 -- AI summary of results
    raw_results   JSONB,                -- raw brave/google output
    source_urls   TEXT[],               -- list of visited URLs
    agent_id      VARCHAR(64),          -- who asked?
    embedding     vector(4096),         -- for semantic recall ("similar queries")
    staleness_score FLOAT DEFAULT 1.0,  -- decay factor (1.0 = fresh)
    ttl_hours     INTEGER DEFAULT 168,  -- default 7 days validity
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ GENERATED ALWAYS AS (created_at + (ttl_hours || ' hours')::interval) STORED
);

-- Indexes for search cache
CREATE INDEX IF NOT EXISTS idx_searches_query_hash ON web_searches (query_hash);
CREATE INDEX IF NOT EXISTS idx_searches_embedding ON web_searches USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_searches_agent_id ON web_searches (agent_id);
CREATE INDEX IF NOT EXISTS idx_searches_created ON web_searches (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_searches_expires ON web_searches (expires_at);

-- 2. Audit Log (Strict Mandate)
CREATE TABLE IF NOT EXISTS audit_logs (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id      VARCHAR(64) NOT NULL,
    action_type   VARCHAR(64) NOT NULL, -- e.g., 'web_search', 'code_edit', 'deploy'
    tool_name     VARCHAR(64),          -- e.g., 'browser', 'fs'
    status        VARCHAR(20) NOT NULL CHECK (status IN ('success', 'failure', 'pending')),
    input_params  JSONB,                -- what was requested
    output_summary TEXT,                -- brief result
    metadata      JSONB DEFAULT '{}',   -- context, session_id, etc.
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for audit log
CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_logs (agent_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs (action_type);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs (created_at DESC);
-- Partitioning by time could be considered later for massive scale, but simple index is fine for now.
