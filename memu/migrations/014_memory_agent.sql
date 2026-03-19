-- Migration 014: Memory Agent — HNSW index, FTS, pg_trgm, compaction layer, concept index
-- Non-destructive: adds tables/indexes, never removes memories

-- 1. Fast text search extensions
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 2. FTS tsvector column on memories
ALTER TABLE memories ADD COLUMN IF NOT EXISTS fts_vector tsvector;

-- Backfill existing rows
UPDATE memories SET fts_vector = to_tsvector('english', content) WHERE fts_vector IS NULL;

-- FTS trigger for future inserts/updates
CREATE OR REPLACE FUNCTION memories_fts_trigger_fn()
RETURNS TRIGGER AS $$
BEGIN
    NEW.fts_vector := to_tsvector('english', NEW.content);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS memories_fts_update ON memories;
CREATE TRIGGER memories_fts_update
    BEFORE INSERT OR UPDATE OF content ON memories
    FOR EACH ROW EXECUTE FUNCTION memories_fts_trigger_fn();

-- 3. GIN index for FTS
CREATE INDEX IF NOT EXISTS idx_memories_fts ON memories USING GIN(fts_vector);

-- 4. pg_trgm index for fast ILIKE/grep
CREATE INDEX IF NOT EXISTS idx_memories_trgm ON memories USING GIN(content gin_trgm_ops);

-- 5. HNSW vector index — only for dims <= 2000 (pgvector limitation)
-- For >2000 dims (e.g. qwen3-embedding 4096), rely on ivfflat instead.
DO $$
DECLARE
    dim_size INT;
BEGIN
    SELECT atttypmod FROM pg_attribute
      WHERE attrelid = 'memories'::regclass AND attname = 'embedding'
      INTO dim_size;
    IF dim_size IS NOT NULL AND dim_size <= 2000 THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_memories_hnsw ON memories USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)';
    END IF;
END
$$;

-- 6. Compactions table — hierarchical summary nodes (NEVER replaces original memories)
CREATE TABLE IF NOT EXISTS memory_compactions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    summary             TEXT NOT NULL,
    embedding           vector(4096),
    fts_vector          tsvector,
    memory_type         VARCHAR(20) DEFAULT 'reflection',
    agent_id            VARCHAR(64),
    source_memory_ids   UUID[] NOT NULL DEFAULT '{}',
    source_compaction_ids UUID[] DEFAULT '{}',
    compaction_level    INTEGER DEFAULT 1,  -- 1=episodic→semantic, 2=semantic→strategic
    concept_tags        TEXT[] DEFAULT '{}',
    date_range_start    TIMESTAMPTZ,
    date_range_end      TIMESTAMPTZ,
    metadata            JSONB DEFAULT '{}',
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- FTS trigger for compactions
CREATE OR REPLACE FUNCTION compactions_fts_trigger_fn()
RETURNS TRIGGER AS $$
BEGIN
    NEW.fts_vector := to_tsvector('english', NEW.summary);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS compactions_fts_update ON memory_compactions;
CREATE TRIGGER compactions_fts_update
    BEFORE INSERT OR UPDATE OF summary ON memory_compactions
    FOR EACH ROW EXECUTE FUNCTION compactions_fts_trigger_fn();

-- Vector index for compactions — skip if dims > 2000 (pgvector HNSW/ivfflat limit)
DO $$
BEGIN
    BEGIN
        CREATE INDEX IF NOT EXISTS idx_compactions_hnsw
            ON memory_compactions USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'Skipping compactions vector index (dims > 2000 limit): %', SQLERRM;
    END;
END
$$;

CREATE INDEX IF NOT EXISTS idx_compactions_fts
    ON memory_compactions USING GIN(fts_vector);

CREATE INDEX IF NOT EXISTS idx_compactions_source
    ON memory_compactions USING GIN(source_memory_ids);

CREATE INDEX IF NOT EXISTS idx_compactions_tags
    ON memory_compactions USING GIN(concept_tags);

CREATE INDEX IF NOT EXISTS idx_compactions_level
    ON memory_compactions(compaction_level);

CREATE INDEX IF NOT EXISTS idx_compactions_agent
    ON memory_compactions(agent_id, compaction_level);

-- 7. Concept index — fast grep-like lookup without vector search
CREATE TABLE IF NOT EXISTS memory_concept_index (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    concept     TEXT NOT NULL,
    memory_ids  UUID[] NOT NULL DEFAULT '{}',
    compaction_ids UUID[] DEFAULT '{}',
    frequency   INTEGER DEFAULT 1,
    last_seen   TIMESTAMPTZ DEFAULT NOW(),
    metadata    JSONB DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_concept_index_concept
    ON memory_concept_index(concept);

CREATE INDEX IF NOT EXISTS idx_concept_trgm
    ON memory_concept_index USING GIN(concept gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_concept_frequency
    ON memory_concept_index(frequency DESC);

-- 8. Agent run log — track idle agent activity
CREATE TABLE IF NOT EXISTS memory_agent_runs (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_type            VARCHAR(32) NOT NULL,   -- 'compaction','indexing','distillation','full'
    status              VARCHAR(16) DEFAULT 'running',  -- 'running','done','failed'
    memories_processed  INTEGER DEFAULT 0,
    compactions_created INTEGER DEFAULT 0,
    concepts_indexed    INTEGER DEFAULT 0,
    model_used          VARCHAR(64),
    started_at          TIMESTAMPTZ DEFAULT NOW(),
    finished_at         TIMESTAMPTZ,
    error               TEXT,
    metadata            JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_started
    ON memory_agent_runs(started_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_runs_status
    ON memory_agent_runs(status, run_type);
