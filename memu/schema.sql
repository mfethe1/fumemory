-- memU schema — Postgres + pgvector

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS memories (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    content       TEXT NOT NULL,
    embedding     vector(1536),
    memory_type   VARCHAR(20) NOT NULL DEFAULT 'fact'
                  CHECK (memory_type IN ('fact', 'decision', 'lesson', 'pattern', 'failure')),
    agent_id      VARCHAR(64),
    metadata      JSONB NOT NULL DEFAULT '{}',
    parent_id     UUID REFERENCES memories(id) ON DELETE SET NULL,
    confidence    FLOAT NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    access_count  INTEGER NOT NULL DEFAULT 0,
    decay_score   FLOAT NOT NULL DEFAULT 1.0,
    content_hash  VARCHAR(64),  -- for deduplication
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_memories_agent_id ON memories (agent_id);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories (memory_type);
CREATE INDEX IF NOT EXISTS idx_memories_parent ON memories (parent_id);
CREATE INDEX IF NOT EXISTS idx_memories_content_hash ON memories (content_hash);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_decay ON memories (decay_score DESC);

-- API keys table
CREATE TABLE IF NOT EXISTS api_keys (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    key_hash   VARCHAR(128) NOT NULL UNIQUE,
    name       VARCHAR(128),
    agent_id   VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ
);

-- Update trigger for updated_at
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER memories_updated_at
    BEFORE UPDATE ON memories
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();
