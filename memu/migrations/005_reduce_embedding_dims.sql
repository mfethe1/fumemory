-- Compatibility migration: add 4096-dim search_history embedding storage.
-- ADR-0003 requires dimension changes to be additive and versioned.

ALTER TABLE search_history
    ADD COLUMN IF NOT EXISTS embedding_version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE search_history
    ADD COLUMN IF NOT EXISTS embedding_v4096 vector(4096);

COMMENT ON COLUMN search_history.embedding_version IS
    'Tracks which embedding contract generated the stored search vector.';

COMMENT ON COLUMN search_history.embedding_v4096 IS
    'Additive 4096-dim embedding column. Existing search_history.embedding values are preserved.';

CREATE INDEX IF NOT EXISTS idx_search_history_embedding_version
    ON search_history(embedding_version)
    WHERE embedding IS NOT NULL OR embedding_v4096 IS NOT NULL;
