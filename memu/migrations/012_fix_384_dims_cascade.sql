-- Migration: Fix 384-dim embeddings by dropping dependent views first (CASCADE)
-- view current_memories depends on memories.embedding

DROP INDEX IF EXISTS idx_memories_embedding;

-- Drop dependent views first
DROP VIEW IF EXISTS memory_link_stats;
DROP VIEW IF EXISTS current_memories;

-- Now drop and recreate embedding column
ALTER TABLE memories DROP COLUMN IF EXISTS embedding;
ALTER TABLE memories ADD COLUMN embedding vector(384);

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

-- Fix search_history too
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='search_history' AND column_name='embedding') THEN
    ALTER TABLE search_history DROP COLUMN embedding;
    ALTER TABLE search_history ADD COLUMN embedding vector(384);
  END IF;
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'search_history note: %', SQLERRM;
END
$$;

-- Recreate HNSW index
CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories USING hnsw (embedding vector_cosine_ops);
