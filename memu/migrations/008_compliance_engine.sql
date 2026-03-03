-- Migration: Compliance Engine foundation
-- Adds cryptographic signing support, failure taxonomy, and forensics fields

-- 1. Expand signature column for Ed25519 (128 hex chars + overhead)
ALTER TABLE events ALTER COLUMN signature TYPE VARCHAR(512);

-- 2. Add public key registration to gateway_registry
ALTER TABLE gateway_registry ADD COLUMN IF NOT EXISTS public_key BYTEA;
ALTER TABLE gateway_registry ADD COLUMN IF NOT EXISTS key_registered_at TIMESTAMPTZ;

-- 3. Add failure taxonomy to dead_letter_queue
ALTER TABLE dead_letter_queue ADD COLUMN IF NOT EXISTS failure_category VARCHAR(40)
    DEFAULT 'unknown'
    CHECK (failure_category IN (
        'hallucination', 'api_timeout', 'logic_loop',
        'unauthorized_data_access', 'guardrail_violation',
        'resource_exhaustion', 'dependency_failure',
        'schema_violation', 'split_brain', 'unknown'
    ));
ALTER TABLE dead_letter_queue ADD COLUMN IF NOT EXISTS failure_subcategory VARCHAR(128);

-- 4. Add version_hash to memories for Proof of Context
ALTER TABLE memories ADD COLUMN IF NOT EXISTS version_hash VARCHAR(64);

-- 5. Index for actuarial queries (failure rate by category)
CREATE INDEX IF NOT EXISTS idx_dlq_category ON dead_letter_queue (failure_category)
    WHERE resolved_at IS NULL;

-- 6. Add 'merkle_anchor' to events event_type CHECK
-- We need to drop and recreate the constraint to add new values
DO $$
BEGIN
    ALTER TABLE events DROP CONSTRAINT IF EXISTS events_event_type_check;
    ALTER TABLE events ADD CONSTRAINT events_event_type_check
        CHECK (event_type IN (
            'task_drafted', 'task_amended', 'task_claimed',
            'task_completed', 'task_failed', 'task_cancelled',
            'bid_submitted', 'lease_granted', 'lease_expired',
            'audit_proposed', 'audit_accepted', 'audit_rejected',
            'circuit_breaker', 'system_halt', 'system_override',
            'heartbeat',
            'lane_acquired', 'lane_released', 'lane_contested',
            'task_orphaned', 'task_hydrated', 'checkpoint_saved',
            'rollback_executed',
            'dlq_enqueued', 'dlq_diagnosed', 'dlq_healed',
            'rpc_request', 'rpc_response',
            'memory_written', 'decision_made', 'health_check',
            'merkle_anchor'
        ));
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Event type constraint update skipped: %', SQLERRM;
END
$$;
