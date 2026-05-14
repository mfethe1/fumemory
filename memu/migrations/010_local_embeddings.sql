-- Migration: add 384-dim embedding storage for local fastembed models.
-- ADR-0003 requires dimension changes to be additive and versioned.

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS embedding_version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS embedding_v384 vector(384);

COMMENT ON COLUMN memories.embedding_v384 IS
    'Additive 384-dim embedding column for local fastembed models. Existing memories.embedding values are preserved.';

CREATE INDEX IF NOT EXISTS idx_memories_embedding_v384
    ON memories USING hnsw (embedding_v384 vector_cosine_ops);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'search_history') THEN
    ALTER TABLE search_history
        ADD COLUMN IF NOT EXISTS embedding_version INTEGER NOT NULL DEFAULT 1;

    ALTER TABLE search_history
        ADD COLUMN IF NOT EXISTS embedding_v384 vector(384);

    COMMENT ON COLUMN search_history.embedding_v384 IS
        'Additive 384-dim embedding column for local fastembed models. Existing search_history.embedding values are preserved.';

    EXECUTE 'CREATE INDEX IF NOT EXISTS idx_search_history_embedding_v384 ON search_history USING hnsw (embedding_v384 vector_cosine_ops)';
  END IF;
END
$$;
