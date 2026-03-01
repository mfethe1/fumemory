-- Compatibility migration: enforce 4096-dim embeddings for search_history.
-- Note: pgvector index backends have limits; index creation is handled elsewhere.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema='public'
      AND table_name='search_history'
      AND column_name='embedding'
  ) THEN
    ALTER TABLE search_history ALTER COLUMN embedding TYPE vector(4096);
  END IF;
END
$$;

DROP INDEX IF EXISTS idx_search_history_embedding;
