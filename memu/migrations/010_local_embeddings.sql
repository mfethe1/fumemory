-- Migration: Switch embedding columns to 384 dims for local fastembed model (BAAI/bge-small-en-v1.5)
-- We are dropping OpenAI dependency and defaulting to local on Railway.

DROP INDEX IF EXISTS idx_memories_embedding;
DROP INDEX IF EXISTS idx_search_history_embedding;

DO $$
BEGIN
  -- Clear out old high-dim vectors since they are incompatible
  UPDATE memories SET embedding = NULL WHERE embedding IS NOT NULL;
  UPDATE search_history SET embedding = NULL WHERE embedding IS NOT NULL;
  
  -- Alter columns to 384
  ALTER TABLE memories ALTER COLUMN embedding TYPE vector(384);
  ALTER TABLE search_history ALTER COLUMN embedding TYPE vector(384);
  
  RAISE NOTICE 'memories and search_history embedding upgraded to vector(384)';
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'embedding migration skipped: %', SQLERRM;
END
$$;
