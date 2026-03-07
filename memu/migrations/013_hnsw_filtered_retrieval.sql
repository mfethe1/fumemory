-- Migration 013: Upgrade IVFFlat → HNSW index for filtered ANN retrieval
-- Motivation: HNSW supports efficient pre-filtering by agent_id/memory_type
-- which eliminates the post-filter accuracy loss we get with IVFFlat.
-- pgvector >= 0.7.0 required (HNSW support), 0.8+ recommended for filtered search.

-- Step 1: Drop the old IVFFlat index
DROP INDEX IF EXISTS idx_memories_embedding;

-- Step 2: Create HNSW index on embedding column
-- m=16, ef_construction=200 are good defaults for our ~3000 memory corpus.
-- For larger corpora (>100k), increase ef_construction to 256-512.
CREATE INDEX idx_memories_embedding_hnsw
    ON memories USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

-- Step 3: Create composite indexes for common filtered queries
-- These let pgvector push agent_id/memory_type filters INTO the ANN scan
-- instead of post-filtering (which discards valid nearest neighbors).

-- Agent-scoped search (most common query pattern for multi-agent)
CREATE INDEX IF NOT EXISTS idx_memories_agent_created
    ON memories (agent_id, created_at DESC);

-- Type-scoped search (e.g., "find all lessons")
CREATE INDEX IF NOT EXISTS idx_memories_type_created
    ON memories (memory_type, created_at DESC);

-- Confidence-filtered search
CREATE INDEX IF NOT EXISTS idx_memories_confidence
    ON memories (confidence DESC)
    WHERE confidence >= 0.5;

-- Step 4: Set HNSW search parameters for quality
-- ef (search-time beam width) trades speed for recall accuracy.
-- Default 40 is too low for filtered search; 100 gives good recall without
-- significant latency increase at our corpus size.
-- This is a session-level setting; we'll set it in the application connection pool.
-- ALTER SYSTEM SET hnsw.ef_search = 100;  -- uncomment if you want cluster-wide

COMMENT ON INDEX idx_memories_embedding_hnsw IS
    'HNSW index for filtered ANN retrieval. Replaces IVFFlat (migration 013). m=16, ef_construction=200.';
