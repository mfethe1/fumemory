-- Migration: add 4096-dim memories embedding storage.
-- ADR-0003 requires dimension changes to be additive and versioned.

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS embedding_version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS embedding_v4096 vector(4096);

COMMENT ON COLUMN memories.embedding_version IS
    'Tracks which embedding contract generated the stored vector.';

COMMENT ON COLUMN memories.embedding_v4096 IS
    'Additive 4096-dim embedding column. Existing memories.embedding values are preserved.';

CREATE INDEX IF NOT EXISTS idx_memories_embedding_version
    ON memories(embedding_version)
    WHERE embedding IS NOT NULL OR embedding_v4096 IS NOT NULL;
