-- Migration: Ensure embedding columns are 384 dims (corrects 010 if it silently failed)
-- Safe to run multiple times.

DROP INDEX IF EXISTS idx_memories_embedding;
DROP INDEX IF EXISTS idx_search_history_embedding;

-- Force drop+recreate for memories
ALTER TABLE memories DROP COLUMN IF EXISTS embedding;
ALTER TABLE memories ADD COLUMN embedding vector(384);

-- Force drop+recreate for search_history  
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'search_history') THEN
    ALTER TABLE search_history DROP COLUMN IF EXISTS embedding;
    ALTER TABLE search_history ADD COLUMN embedding vector(384);
    RAISE NOTICE 'search_history.embedding resized to 384';
  END IF;
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'search_history migration note: %', SQLERRM;
END
$$;

-- Recreate HNSW index
CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories USING hnsw (embedding vector_cosine_ops);
