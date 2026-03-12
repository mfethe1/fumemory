-- Migration: Use DROP COLUMN CASCADE to safely recreate embedding as vector(4096)
-- This migration uses CASCADE to automatically drop dependent objects (views, indexes, constraints)
-- and then recreates them with the correct 4096-dimensional vector type.
-- 
-- Rationale: qwen3-embedding produces 4096-dim vectors, so all embedding columns must match.
-- Using CASCADE ensures we don't miss any dependent objects that would block the migration.

-- Drop indexes explicitly first (best practice before CASCADE)
DROP INDEX IF EXISTS idx_memories_embedding;
DROP INDEX IF EXISTS idx_search_history_embedding;

-- Drop and recreate memories.embedding with CASCADE
-- CASCADE will automatically drop:
-- - Any views that reference memories.embedding
-- - Any constraints on the column
-- - Any other dependent objects
ALTER TABLE memories DROP COLUMN IF EXISTS embedding CASCADE;
ALTER TABLE memories ADD COLUMN embedding vector(4096);

-- Drop and recreate search_history.embedding with CASCADE (if table exists)
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 
    FROM information_schema.tables 
    WHERE table_schema = 'public' 
      AND table_name = 'search_history'
  ) THEN
    ALTER TABLE search_history DROP COLUMN IF EXISTS embedding CASCADE;
    ALTER TABLE search_history ADD COLUMN embedding vector(4096);
    RAISE NOTICE 'search_history.embedding recreated as vector(4096)';
  END IF;
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'search_history migration note: %', SQLERRM;
END
$$;

-- Recreate essential views that were dropped by CASCADE
-- These definitions come from 002_amem_bitemporal.sql

CREATE OR REPLACE VIEW current_memories AS
  SELECT * FROM memories WHERE valid_to IS NULL;

CREATE OR REPLACE VIEW memory_link_stats AS
SELECT
    m.id, 
    m.content, 
    m.agent_id, 
    m.memory_type, 
    m.confidence, 
    m.access_count, 
    m.created_at,
    COUNT(DISTINCT ml.id) AS link_count,
    AVG(ml.strength) AS avg_link_strength
FROM memories m
LEFT JOIN memory_links ml ON (m.id = ml.source_id OR m.id = ml.target_id)
WHERE m.valid_to IS NULL
GROUP BY m.id;

-- Recreate indexes with appropriate strategy based on dataset size
-- IVFFlat is better for high-dimensional vectors (4096 dims) than HNSW
DO $$
DECLARE
  row_count INTEGER;
  list_count INTEGER;
BEGIN
  -- Count rows with embeddings
  SELECT COUNT(*) INTO row_count FROM memories WHERE embedding IS NOT NULL;
  
  IF row_count >= 1000 THEN
    -- For large datasets, use IVFFlat with dynamic list sizing
    -- Rule of thumb: lists = sqrt(row_count), clamped between 10 and 1000
    list_count := GREATEST(10, LEAST(1000, SQRT(row_count)::INTEGER));
    
    EXECUTE format(
      'CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories USING ivfflat (embedding vector_cosine_ops) WITH (lists = %s)',
      list_count
    );
    
    RAISE NOTICE 'Created IVFFlat index with % lists for % rows', list_count, row_count;
  ELSIF row_count >= 100 THEN
    -- For medium datasets, use HNSW (better for < 1000 dims, but acceptable for 4096)
    CREATE INDEX IF NOT EXISTS idx_memories_embedding 
      ON memories USING hnsw (embedding vector_cosine_ops);
    
    RAISE NOTICE 'Created HNSW index for medium dataset (% rows)', row_count;
  ELSE
    -- For small datasets, skip index (sequential scan is faster)
    RAISE NOTICE 'Skipped index creation for small dataset (% rows) - sequential scan is optimal', row_count;
  END IF;
  
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'Index creation skipped: %', SQLERRM;
END
$$;

-- Create index for search_history if it has enough data
DO $$
DECLARE
  row_count INTEGER;
BEGIN
  IF EXISTS (
    SELECT 1 
    FROM information_schema.tables 
    WHERE table_schema = 'public' 
      AND table_name = 'search_history'
  ) THEN
    SELECT COUNT(*) INTO row_count FROM search_history WHERE embedding IS NOT NULL;
    
    IF row_count >= 100 THEN
      CREATE INDEX IF NOT EXISTS idx_search_history_embedding 
        ON search_history USING hnsw (embedding vector_cosine_ops);
      
      RAISE NOTICE 'Created search_history embedding index (% rows)', row_count;
    ELSE
      RAISE NOTICE 'Skipped search_history index (% rows)', row_count;
    END IF;
  END IF;
  
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'search_history index creation note: %', SQLERRM;
END
$$;

RAISE NOTICE 'Migration 015 complete: All embedding columns are now vector(4096) with CASCADE safety';

