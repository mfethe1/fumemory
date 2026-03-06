-- Migration: Fix 384-dim embeddings by dropping dependent views first
-- The view current_memories depends on memories.embedding, so we drop cascade.

DROP INDEX IF EXISTS idx_memories_embedding;

-- Drop with CASCADE to handle dependent views
ALTER TABLE memories DROP COLUMN IF EXISTS embedding CASCADE;
ALTER TABLE memories ADD COLUMN embedding vector(384);

-- Recreate the current_memories view (re-includes new embedding column)
CREATE OR REPLACE VIEW current_memories AS
  SELECT * FROM memories WHERE deleted_at IS NULL;

-- Fix search_history too
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='search_history' AND column_name='embedding') THEN
    ALTER TABLE search_history DROP COLUMN embedding CASCADE;
    ALTER TABLE search_history ADD COLUMN embedding vector(384);
  END IF;
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'search_history note: %', SQLERRM;
END
$$;

-- Recreate HNSW index (384 dims well within pgvector limits)
CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories USING hnsw (embedding vector_cosine_ops);
