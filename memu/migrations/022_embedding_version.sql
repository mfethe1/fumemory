-- Migration 022: Additive embedding version tracking
--
-- Adds embedding_version to memories so future dimension changes can add a
-- new vector column or table and reindex forward without dropping existing
-- vectors. This migration is purely additive: no columns are dropped and no
-- stored embeddings are modified.
--
-- embedding_version = 1  →  initial contract (existing rows, any dimension)
-- Future contracts increment this and introduce a new versioned vector column.

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS embedding_version INTEGER NOT NULL DEFAULT 1;

COMMENT ON COLUMN memories.embedding_version IS
    'Tracks which embedding contract generated the stored vector. '
    'Version 1 covers all vectors created before this migration. '
    'Future dimension changes add a new versioned column and increment this.';

-- Back-fill existing rows that have an embedding stored
UPDATE memories
    SET embedding_version = 1
    WHERE embedding IS NOT NULL
      AND embedding_version IS DISTINCT FROM 1;

-- Index for efficient per-version recall when multiple versions co-exist
CREATE INDEX IF NOT EXISTS idx_memories_embedding_version
    ON memories(embedding_version)
    WHERE embedding IS NOT NULL;
