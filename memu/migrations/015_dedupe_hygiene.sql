-- Migration 015: dedupe + operational hygiene support
-- 1. Add caller-supplied custom_id for idempotent writes
-- 2. Track duplicate_count on surviving memories
-- 3. Enforce unique custom_id per tenant when supplied

ALTER TABLE memories ADD COLUMN IF NOT EXISTS custom_id VARCHAR(191);
ALTER TABLE memories ADD COLUMN IF NOT EXISTS duplicate_count INTEGER NOT NULL DEFAULT 0;

CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_tenant_custom_id_unique
    ON memories (tenant_id, custom_id)
    WHERE custom_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_memories_custom_id ON memories (custom_id) WHERE custom_id IS NOT NULL;
