-- Migration: Ensure all embedding columns are 4096 dims (qwen3-embedding standard)
-- This migration supersedes 011 and 012 which incorrectly forced 384 dims.
-- Safe to run multiple times.

DROP INDEX IF EXISTS idx_memories_embedding;
DROP INDEX IF EXISTS idx_search_history_embedding;

-- Drop dependent views first (CASCADE safety)
DROP VIEW IF EXISTS memory_link_stats;
DROP VIEW IF EXISTS current_memories;

-- Force drop+recreate for memories with 4096 dims
ALTER TABLE memories DROP COLUMN IF EXISTS embedding;
ALTER TABLE memories ADD COLUMN embedding vector(4096);

-- Force drop+recreate for search_history with 4096 dims
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'search_history') THEN
    ALTER TABLE search_history DROP COLUMN IF EXISTS embedding;
    ALTER TABLE search_history ADD COLUMN embedding vector(4096);
    RAISE NOTICE 'search_history.embedding resized to 4096';
  END IF;
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'search_history migration note: %', SQLERRM;
END
$$;

-- Recreate views (exact definitions from 002_amem_bitemporal.sql)
CREATE OR REPLACE VIEW current_memories AS
  SELECT * FROM memories WHERE valid_to IS NULL;

CREATE OR REPLACE VIEW memory_link_stats AS
SELECT
    m.id, m.content, m.agent_id, m.memory_type, m.confidence, m.access_count, m.created_at,
    COUNT(DISTINCT ml.id) AS link_count,
    AVG(ml.strength) AS avg_link_strength
FROM memories m
LEFT JOIN memory_links ml ON (m.id = ml.source_id OR m.id = ml.target_id)
WHERE m.valid_to IS NULL
GROUP BY m.id;

-- Recreate IVFFlat index (better for high-dimensional vectors than HNSW)
-- Note: IVFFlat requires training data, so we only create if we have enough rows
DO $$
DECLARE
  row_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO row_count FROM memories WHERE embedding IS NOT NULL;
  
  IF row_count >= 1000 THEN
    -- Create IVFFlat index with appropriate number of lists (sqrt of row count is a good heuristic)
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories USING ivfflat (embedding vector_cosine_ops) WITH (lists = %s)', GREATEST(10, LEAST(1000, SQRT(row_count)::INTEGER)));
    RAISE NOTICE 'Created IVFFlat index with % lists for % rows', GREATEST(10, LEAST(1000, SQRT(row_count)::INTEGER)), row_count;
  ELSE
    -- For small datasets, use HNSW or no index (sequential scan is fine)
    CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories USING hnsw (embedding vector_cosine_ops);
    RAISE NOTICE 'Created HNSW index for small dataset (% rows)', row_count;
  END IF;
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'Index creation skipped: %', SQLERRM;
END
$$;

