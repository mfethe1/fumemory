-- Migration: Align all embedding columns to 4096 dims
-- qwen3-embedding on Railway produces 4096-dim vectors.
-- The original schema created memories.embedding as vector(1536).
-- This migration aligns everything to the actual model output.

-- Step 1: Drop any indexes that depend on the embedding column type
DROP INDEX IF EXISTS idx_memories_embedding;
DROP INDEX IF EXISTS idx_search_history_embedding;

-- Step 2: Alter memories embedding to 4096 dims
-- Note: existing 1536-dim vectors will be dropped (NULL'd) since they're
-- incompatible. This is acceptable — they'll be re-embedded on next access.
DO $$
BEGIN
  -- Null out any old 1536-dim embeddings (can't cast between vector sizes)
  UPDATE memories SET embedding = NULL WHERE embedding IS NOT NULL;
  
  -- Alter column type
  ALTER TABLE memories ALTER COLUMN embedding TYPE vector(4096);
  
  RAISE NOTICE 'memories.embedding upgraded to vector(4096)';
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'memories embedding migration skipped: %', SQLERRM;
END
$$;

-- Step 3: Recreate indexes (without HNSW for now — 4096 dims may exceed
-- pgvector HNSW limits on some versions; use IVFFlat instead)
-- IVFFlat supports higher dimensions more reliably
-- Skip index creation for now — let queries use sequential scan until
-- we have enough vectors for IVFFlat training (needs ~1000+ rows)
