-- Migration: ensure additive 4096-dim embedding storage exists.
-- ADR-0003 requires dimension changes to be additive and versioned.

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS embedding_version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS embedding_v4096 vector(4096);

COMMENT ON COLUMN memories.embedding_v4096 IS
    'Additive 4096-dim embedding column. Existing memories.embedding values are preserved.';

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'search_history') THEN
    ALTER TABLE search_history
        ADD COLUMN IF NOT EXISTS embedding_version INTEGER NOT NULL DEFAULT 1;

    ALTER TABLE search_history
        ADD COLUMN IF NOT EXISTS embedding_v4096 vector(4096);

    COMMENT ON COLUMN search_history.embedding_v4096 IS
        'Additive 4096-dim embedding column. Existing search_history.embedding values are preserved.';
  END IF;
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'search_history additive embedding migration note: %', SQLERRM;
END
$$;

CREATE INDEX IF NOT EXISTS idx_memories_embedding_version
    ON memories(embedding_version)
    WHERE embedding IS NOT NULL OR embedding_v4096 IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_search_history_embedding_version
    ON search_history(embedding_version)
    WHERE embedding IS NOT NULL OR embedding_v4096 IS NOT NULL;

