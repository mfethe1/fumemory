-- Add unique partial index for dream consolidation idempotency.
-- Only rows with a non-NULL idempotency_key in their JSONB metadata
-- are constrained — this does NOT affect the vast majority of memories
-- that have no idempotency_key.
CREATE UNIQUE INDEX IF NOT EXISTS uq_memories_idempotency_key
ON memories ((metadata->>'idempotency_key'))
WHERE metadata->>'idempotency_key' IS NOT NULL;

