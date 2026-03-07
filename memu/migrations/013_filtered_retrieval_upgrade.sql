-- Migration 013: Filtered Retrieval Upgrade
-- 1. Add tags column for entity/metadata pre-filtering
-- 2. Upgrade IVFFlat → HNSW index (better recall + pre-filter support)
-- 3. Add memory_blocks table (context blocks for stable agent state)
-- 4. Add composite indexes for filtered vector search

-- ============================================================
-- 1. Tags column on memories
-- ============================================================
ALTER TABLE memories ADD COLUMN IF NOT EXISTS tags TEXT[] DEFAULT '{}';

-- GIN index for fast array containment queries (@> operator)
CREATE INDEX IF NOT EXISTS idx_memories_tags ON memories USING GIN (tags);

-- ============================================================
-- 2. HNSW index (replaces IVFFlat for better recall + filtering)
-- ============================================================
-- Drop old IVFFlat index safely
DROP INDEX IF EXISTS idx_memories_embedding;

-- Create HNSW index with cosine distance
-- m=16, ef_construction=200 gives excellent recall for ~3k-50k rows
CREATE INDEX IF NOT EXISTS idx_memories_embedding_hnsw
    ON memories USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

-- ============================================================
-- 3. Context Blocks table
-- ============================================================
CREATE TABLE IF NOT EXISTS memory_blocks (
    key           TEXT NOT NULL,
    content       TEXT NOT NULL,
    agent_owner   VARCHAR(64),
    tenant_id     UUID NOT NULL DEFAULT uuid_generate_v4(),
    metadata      JSONB NOT NULL DEFAULT '{}',
    version       INTEGER NOT NULL DEFAULT 1,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (key, tenant_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_blocks_agent ON memory_blocks (agent_owner);
CREATE INDEX IF NOT EXISTS idx_memory_blocks_updated ON memory_blocks (updated_at DESC);

-- Update trigger for memory_blocks
CREATE OR REPLACE TRIGGER memory_blocks_updated_at
    BEFORE UPDATE ON memory_blocks
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();

-- ============================================================
-- 4. Composite indexes for common filtered searches
-- ============================================================
-- agent_id + embedding (most common filter)
CREATE INDEX IF NOT EXISTS idx_memories_agent_embedding
    ON memories (agent_id)
    WHERE embedding IS NOT NULL;

-- memory_type + created_at (for type-filtered temporal queries)
CREATE INDEX IF NOT EXISTS idx_memories_type_created
    ON memories (memory_type, created_at DESC);

-- Partial index: only non-null embeddings (speeds up all vector searches)
CREATE INDEX IF NOT EXISTS idx_memories_has_embedding
    ON memories (id)
    WHERE embedding IS NOT NULL;
