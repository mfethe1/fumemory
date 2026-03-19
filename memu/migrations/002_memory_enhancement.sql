-- Migration 002: Memory Enhancement
-- Adds: HNSW index, BM25/tsvector, cluster hierarchy (RAPTOR), summary layer
-- Safe to run multiple times (IF NOT EXISTS throughout)

-- 1. HNSW candidate index — use binary quantization for 4096-dim embeddings, then rerank exactly
DROP INDEX IF EXISTS idx_memories_embedding;
DROP INDEX IF EXISTS idx_memories_embedding_hnsw;
DROP INDEX IF EXISTS idx_memories_embedding_halfvec;
CREATE INDEX IF NOT EXISTS idx_memories_embedding_bit_hnsw
    ON memories USING hnsw (((binary_quantize(embedding))::bit(4096)) bit_hamming_ops);

-- 2. Full-text search column — enables BM25-style ranking via ts_rank
ALTER TABLE memories ADD COLUMN IF NOT EXISTS content_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;

CREATE INDEX IF NOT EXISTS idx_memories_tsv ON memories USING GIN (content_tsv);

-- 3. Cluster hierarchy for RAPTOR-style recursive retrieval
CREATE TABLE IF NOT EXISTS memory_clusters (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    level         INTEGER NOT NULL DEFAULT 0,  -- 0=leaf, 1=cluster, 2=supercluster
    summary       TEXT NOT NULL,               -- LLM-generated summary of children
    summary_tsv   tsvector GENERATED ALWAYS AS (to_tsvector('english', summary)) STORED,
    embedding     vector(4096),               -- embedding of the summary
    child_ids     UUID[] NOT NULL DEFAULT '{}',
    parent_id     UUID REFERENCES memory_clusters(id) ON DELETE SET NULL,
    agent_scope   VARCHAR(64),                 -- NULL = global cluster
    memory_count  INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_clusters_level    ON memory_clusters (level);
CREATE INDEX IF NOT EXISTS idx_clusters_embedding_bit_hnsw
    ON memory_clusters USING hnsw (((binary_quantize(embedding))::bit(4096)) bit_hamming_ops);
CREATE INDEX IF NOT EXISTS idx_clusters_tsv      ON memory_clusters USING GIN (summary_tsv);
CREATE INDEX IF NOT EXISTS idx_clusters_parent   ON memory_clusters (parent_id);
CREATE INDEX IF NOT EXISTS idx_clusters_scope    ON memory_clusters (agent_scope);

-- 4. Cluster membership — which cluster does each memory belong to?
ALTER TABLE memories ADD COLUMN IF NOT EXISTS cluster_id UUID REFERENCES memory_clusters(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_memories_cluster ON memories (cluster_id);

-- 5. Memory distillation log — track what the memory agent has processed
CREATE TABLE IF NOT EXISTS memory_distillation_log (
    id           SERIAL PRIMARY KEY,
    run_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    agent        VARCHAR(64) NOT NULL DEFAULT 'memory-agent',
    action       VARCHAR(64) NOT NULL,  -- 'cluster', 'compact', 'index', 'prune-expired'
    affected_ids UUID[] NOT NULL DEFAULT '{}',
    notes        TEXT,
    model_used   VARCHAR(128),
    duration_ms  INTEGER
);

-- 6. Grepping index — materialized keyword index for fast grep-style lookup
-- Stores top keywords per memory for BM25-style acceleration without full tsvector scan
CREATE TABLE IF NOT EXISTS memory_keyword_index (
    memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    keyword   VARCHAR(128) NOT NULL,
    weight    FLOAT NOT NULL DEFAULT 1.0,
    PRIMARY KEY (memory_id, keyword)
);

CREATE INDEX IF NOT EXISTS idx_keywords_keyword ON memory_keyword_index (keyword);
CREATE INDEX IF NOT EXISTS idx_keywords_weight  ON memory_keyword_index (keyword, weight DESC);

-- 7. Update trigger for clusters
CREATE OR REPLACE TRIGGER memory_clusters_updated_at
    BEFORE UPDATE ON memory_clusters
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
