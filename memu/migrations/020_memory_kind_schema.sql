-- Migration 020: Memory Kind Schema Contract
--
-- Introduces:
--   memory_kind       VARCHAR(20)  — discriminates 'evidence' vs 'learning'
--   idempotency_key   VARCHAR(255) — tenant-scoped idempotency column
--   canonical_payload_hash VARCHAR(64) — hash used to detect replay mismatches
--   review_status     VARCHAR(20)  — learning lifecycle state
--
-- Also applies deterministic legacy backfill rules (PRD §Implementation Decisions):
--   lesson, decision, pattern, procedural, fact, reflection, plan, goal → learning/legacy
--   user_action, external, failure → evidence
--   observation WITH OpenClaw execution metadata (task_id/session_id/gateway_id/event_type) → evidence
--   observation WITHOUT OpenClaw execution metadata → learning/legacy

-- ============================================================
-- Part 1: Add memory_kind column
-- ============================================================
ALTER TABLE memories ADD COLUMN IF NOT EXISTS memory_kind VARCHAR(20)
    NOT NULL DEFAULT 'learning'
    CHECK (memory_kind IN ('evidence', 'learning'));

-- ============================================================
-- Part 2: Add dedicated idempotency_key column (tenant-scoped)
-- This is a first-class column so the unique index can span
-- (tenant_id, idempotency_key) without a functional-index cast.
-- The old migration-017 partial index on metadata->>'idempotency_key'
-- covers legacy dream-consolidation rows and is left in place.
-- ============================================================
ALTER TABLE memories ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(255) DEFAULT NULL;

-- ============================================================
-- Part 3: Add canonical_payload_hash for idempotency validation
-- Computed from normalized canonical evidence fields, excluding
-- transport-only fields (ts, ingested_at, allowed_roles).
-- Same key + same hash → exact replay (return existing ID).
-- Same key + different hash → 409 Conflict.
-- ============================================================
ALTER TABLE memories ADD COLUMN IF NOT EXISTS canonical_payload_hash VARCHAR(64) DEFAULT NULL;

-- ============================================================
-- Part 4: Add review_status for Learning Memory lifecycle
-- NULL = unreviewed / not applicable (evidence rows)
-- proposed            — reflection worker output, not yet recalled
-- accepted            — user or policy approved
-- accepted_by_timeout — six-hour window elapsed, auto-integrated
-- rejected            — user denied
-- legacy              — backfilled from pre-schema rows
-- ============================================================
ALTER TABLE memories ADD COLUMN IF NOT EXISTS review_status VARCHAR(20) DEFAULT NULL
    CHECK (review_status IS NULL OR review_status IN (
        'proposed', 'accepted', 'accepted_by_timeout', 'rejected', 'legacy'
    ));

-- ============================================================
-- Part 5: Unique partial index for tenant-scoped idempotency
-- Covers only rows that carry an idempotency_key so the vast
-- majority of rows (no key) are unaffected.
-- ============================================================
CREATE UNIQUE INDEX IF NOT EXISTS uq_memories_tenant_idempotency
    ON memories (tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- ============================================================
-- Part 6: Kind and review_status query indexes
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_memories_kind
    ON memories (memory_kind);

CREATE INDEX IF NOT EXISTS idx_memories_review_status
    ON memories (review_status)
    WHERE review_status IS NOT NULL;

-- ============================================================
-- Part 7: Update memory_type constraint to include 'procedural'
-- (migration 005 omitted it; we align the constraint now)
-- ============================================================
ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_memory_type_check;
ALTER TABLE memories ADD CONSTRAINT memories_memory_type_check
    CHECK (memory_type IN (
        'fact', 'decision', 'lesson', 'pattern', 'failure',
        'observation', 'reflection', 'plan', 'goal',
        'user_action', 'external', 'procedural'
    ));

-- ============================================================
-- Part 8: Deterministic legacy backfill
--
-- Run in a consistent order so overlapping conditions are
-- resolved safely. All updates target rows with the default
-- memory_kind='learning' (or match evidence types explicitly).
-- ============================================================

-- 8a. Clear learning types → learning/legacy
UPDATE memories
SET    memory_kind   = 'learning',
       review_status = 'legacy'
WHERE  memory_type IN (
           'lesson', 'decision', 'pattern', 'procedural',
           'fact', 'reflection', 'plan', 'goal'
       )
  AND  review_status IS NULL;

-- 8b. Clear evidence types → evidence (review_status stays NULL)
UPDATE memories
SET    memory_kind = 'evidence'
WHERE  memory_type IN ('user_action', 'external', 'failure');

-- 8c. Uncertain 'observation' WITH OpenClaw execution metadata → evidence
UPDATE memories
SET    memory_kind = 'evidence'
WHERE  memory_type = 'observation'
  AND  (
           metadata ? 'task_id'
        OR metadata ? 'session_id'
        OR metadata ? 'gateway_id'
        OR metadata ? 'event_type'
       );

-- 8d. Uncertain 'observation' WITHOUT OpenClaw execution metadata → learning/legacy
UPDATE memories
SET    memory_kind   = 'learning',
       review_status = 'legacy'
WHERE  memory_type    = 'observation'
  AND  memory_kind    = 'learning'   -- already defaulted; explicit for clarity
  AND  review_status  IS NULL
  AND  NOT (
           metadata ? 'task_id'
        OR metadata ? 'session_id'
        OR metadata ? 'gateway_id'
        OR metadata ? 'event_type'
       );
