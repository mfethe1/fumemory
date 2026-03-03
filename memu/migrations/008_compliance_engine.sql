-- Migration: Compliance Engine foundation
-- Adds cryptographic signing support, failure taxonomy, and forensics fields
-- NOTE: Some tables (events, gateway_registry, dead_letter_queue) may not exist
-- in all environments. All DDL is wrapped in conditional blocks.

-- 1. Add version_hash to memories for Proof of Context (always exists)
ALTER TABLE memories ADD COLUMN IF NOT EXISTS version_hash VARCHAR(64);

-- 2. Expand events signature column (conditional — events table is swarm-only)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'events') THEN
        ALTER TABLE events ALTER COLUMN signature TYPE VARCHAR(512);
        RAISE NOTICE 'events.signature expanded to VARCHAR(512)';
    END IF;
END $$;

-- 3. Add public key to gateway_registry (conditional)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'gateway_registry') THEN
        ALTER TABLE gateway_registry ADD COLUMN IF NOT EXISTS public_key BYTEA;
        ALTER TABLE gateway_registry ADD COLUMN IF NOT EXISTS key_registered_at TIMESTAMPTZ;
        RAISE NOTICE 'gateway_registry: public_key columns added';
    END IF;
END $$;

-- 4. Add failure taxonomy to dead_letter_queue (conditional)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'dead_letter_queue') THEN
        ALTER TABLE dead_letter_queue ADD COLUMN IF NOT EXISTS failure_category VARCHAR(40) DEFAULT 'unknown';
        ALTER TABLE dead_letter_queue ADD COLUMN IF NOT EXISTS failure_subcategory VARCHAR(128);
        CREATE INDEX IF NOT EXISTS idx_dlq_category ON dead_letter_queue (failure_category)
            WHERE resolved_at IS NULL;
        RAISE NOTICE 'dead_letter_queue: failure taxonomy columns added';
    END IF;
END $$;

-- 5. Expand events event_type CHECK constraint (conditional)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'events') THEN
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
        RAISE NOTICE 'events: event_type constraint updated with merkle_anchor';
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Event type constraint update skipped: %', SQLERRM;
END $$;
