-- Migration 001: RLS Multi-Tenancy
-- Adds tenant_id to all data tables and enforces Postgres Row-Level Security.
-- SAFE to run on existing DBs: uses IF NOT EXISTS / DO $$ blocks.
-- Run as superuser (Railway Postgres default user qualifies).

BEGIN;

-- ============================================================
-- STEP 1: Add tenant_id to all data tables
-- ============================================================

-- memories
ALTER TABLE memories ADD COLUMN IF NOT EXISTS tenant_id UUID;
UPDATE memories SET tenant_id = uuid_generate_v4() WHERE tenant_id IS NULL;
ALTER TABLE memories ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE memories ALTER COLUMN tenant_id SET DEFAULT uuid_generate_v4();

-- events
ALTER TABLE events ADD COLUMN IF NOT EXISTS tenant_id UUID;
UPDATE events SET tenant_id = uuid_generate_v4() WHERE tenant_id IS NULL;
ALTER TABLE events ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE events ALTER COLUMN tenant_id SET DEFAULT uuid_generate_v4();

-- tasks
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS tenant_id UUID;
UPDATE tasks SET tenant_id = uuid_generate_v4() WHERE tenant_id IS NULL;
ALTER TABLE tasks ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE tasks ALTER COLUMN tenant_id SET DEFAULT uuid_generate_v4();

-- checkpoints
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS tenant_id UUID;
UPDATE checkpoints SET tenant_id = uuid_generate_v4() WHERE tenant_id IS NULL;
ALTER TABLE checkpoints ALTER COLUMN tenant_id SET NOT NULL;

-- blackboard
ALTER TABLE blackboard ADD COLUMN IF NOT EXISTS tenant_id UUID;
UPDATE blackboard SET tenant_id = uuid_generate_v4() WHERE tenant_id IS NULL;
ALTER TABLE blackboard ALTER COLUMN tenant_id SET NOT NULL;

-- dead_letter_queue: no tenant_id needed (references tasks which has it)
-- lane_locks: no tenant_id needed (references tasks)

-- ============================================================
-- STEP 2: Add tenant_id to api_keys table
-- ============================================================
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS tenant_id UUID;
UPDATE api_keys SET tenant_id = uuid_generate_v4() WHERE tenant_id IS NULL;
ALTER TABLE api_keys ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE api_keys ALTER COLUMN tenant_id SET DEFAULT uuid_generate_v4();

-- Add index for fast tenant lookup by key_hash
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON api_keys (tenant_id);

-- ============================================================
-- STEP 3: Add tenant indexes
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_memories_tenant ON memories (tenant_id);
CREATE INDEX IF NOT EXISTS idx_events_tenant ON events (tenant_id);
CREATE INDEX IF NOT EXISTS idx_tasks_tenant ON tasks (tenant_id);
CREATE INDEX IF NOT EXISTS idx_checkpoints_tenant ON checkpoints (tenant_id);
CREATE INDEX IF NOT EXISTS idx_blackboard_tenant ON blackboard (tenant_id);

-- ============================================================
-- STEP 4: Create app_user role (non-superuser for RLS enforcement)
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user NOLOGIN;
    END IF;
END
$$;

-- Grant data access to app_user
GRANT SELECT, INSERT, UPDATE, DELETE ON memories, events, tasks, checkpoints, blackboard, lane_locks, fencing_tokens, dead_letter_queue, checkpoints, root_prompts, gateway_registry, container_registry TO app_user;
GRANT SELECT ON api_keys TO app_user;

-- ============================================================
-- STEP 5: Enable RLS on all tenant-scoped tables
-- ============================================================
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE blackboard ENABLE ROW LEVEL SECURITY;

-- Superuser bypasses RLS by default (safe for migrations/admin)
-- app_user is subject to RLS policies

-- ============================================================
-- STEP 6: Create tenant isolation policies
-- ============================================================

-- DROP existing policies if re-running migration
DO $$ BEGIN
    DROP POLICY IF EXISTS tenant_isolation ON memories;
    DROP POLICY IF EXISTS tenant_isolation ON events;
    DROP POLICY IF EXISTS tenant_isolation ON tasks;
    DROP POLICY IF EXISTS tenant_isolation ON checkpoints;
    DROP POLICY IF EXISTS tenant_isolation ON blackboard;
EXCEPTION WHEN undefined_object THEN NULL;
END $$;

-- memories: tenant sees only their rows
CREATE POLICY tenant_isolation ON memories
    FOR ALL
    TO app_user
    USING (tenant_id = current_setting('app.current_tenant', TRUE)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant', TRUE)::UUID);

-- events: tenant sees only their events
CREATE POLICY tenant_isolation ON events
    FOR ALL
    TO app_user
    USING (tenant_id = current_setting('app.current_tenant', TRUE)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant', TRUE)::UUID);

-- tasks: tenant sees only their tasks
CREATE POLICY tenant_isolation ON tasks
    FOR ALL
    TO app_user
    USING (tenant_id = current_setting('app.current_tenant', TRUE)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant', TRUE)::UUID);

-- checkpoints: via task ownership
CREATE POLICY tenant_isolation ON checkpoints
    FOR ALL
    TO app_user
    USING (tenant_id = current_setting('app.current_tenant', TRUE)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant', TRUE)::UUID);

-- blackboard: global (shared knowledge) — no tenant isolation by default
-- but tenant_id is stored for audit purposes
-- If you want per-tenant blackboards, uncomment below:
-- CREATE POLICY tenant_isolation ON blackboard
--     FOR ALL TO app_user
--     USING (tenant_id = current_setting('app.current_tenant', TRUE)::UUID);

-- ============================================================
-- STEP 7: Tenant lookup helper function
-- ============================================================
CREATE OR REPLACE FUNCTION get_tenant_id_for_key(p_key_hash VARCHAR)
RETURNS UUID AS $$
    SELECT tenant_id FROM api_keys
    WHERE key_hash = p_key_hash
      AND revoked_at IS NULL
    LIMIT 1;
$$ LANGUAGE sql STABLE SECURITY DEFINER;

COMMIT;
