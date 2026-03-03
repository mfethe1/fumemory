-- Migration 009: RLS Multi-Tenancy for memU API tables
-- This is the production-safe version that only targets tables that exist
-- in the memU API database (memories, search_history, backlog, api_keys).
-- Swarm-only tables (events, tasks, checkpoints, blackboard) are handled
-- conditionally for full-stack deployments.

-- ============================================================
-- STEP 1: Create tenants table
-- ============================================================
CREATE TABLE IF NOT EXISTS tenants (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name          VARCHAR(256) NOT NULL,
    slug          VARCHAR(64) NOT NULL UNIQUE,
    plan          VARCHAR(20) NOT NULL DEFAULT 'free'
                  CHECK (plan IN ('free', 'starter', 'pro', 'enterprise')),
    metadata      JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Default tenant for existing data (single-tenant → multi-tenant migration)
INSERT INTO tenants (id, name, slug, plan)
VALUES ('00000000-0000-0000-0000-000000000001', 'Protelynx', 'protelynx', 'enterprise')
ON CONFLICT (slug) DO NOTHING;

-- ============================================================
-- STEP 2: Add tenant_id to core API tables
-- ============================================================

-- memories (always exists)
ALTER TABLE memories ADD COLUMN IF NOT EXISTS tenant_id UUID
    DEFAULT '00000000-0000-0000-0000-000000000001'
    REFERENCES tenants(id);
UPDATE memories SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;

-- search_history (may exist from migration 003)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'search_history') THEN
        ALTER TABLE search_history ADD COLUMN IF NOT EXISTS tenant_id UUID
            DEFAULT '00000000-0000-0000-0000-000000000001';
        UPDATE search_history SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
    END IF;
END $$;

-- backlog (created in migration 006)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'backlog') THEN
        ALTER TABLE backlog ADD COLUMN IF NOT EXISTS tenant_id UUID
            DEFAULT '00000000-0000-0000-0000-000000000001'
            REFERENCES tenants(id);
        UPDATE backlog SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;
    END IF;
END $$;

-- api_keys: add tenant_id for key→tenant resolution
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS tenant_id UUID
    DEFAULT '00000000-0000-0000-0000-000000000001'
    REFERENCES tenants(id);
UPDATE api_keys SET tenant_id = '00000000-0000-0000-0000-000000000001' WHERE tenant_id IS NULL;

-- ============================================================
-- STEP 3: Tenant indexes
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_memories_tenant ON memories (tenant_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON api_keys (tenant_id);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'search_history') THEN
        CREATE INDEX IF NOT EXISTS idx_search_history_tenant ON search_history (tenant_id);
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'backlog') THEN
        CREATE INDEX IF NOT EXISTS idx_backlog_tenant ON backlog (tenant_id);
    END IF;
END $$;

-- ============================================================
-- STEP 4: Create app_user role for RLS enforcement
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user NOLOGIN;
    END IF;
END $$;

-- Grant data access to app_user on tables that exist
GRANT SELECT, INSERT, UPDATE, DELETE ON memories TO app_user;
GRANT SELECT ON api_keys TO app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON tenants TO app_user;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'search_history') THEN
        EXECUTE 'GRANT SELECT, INSERT ON search_history TO app_user';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'backlog') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON backlog TO app_user';
    END IF;
END $$;

-- ============================================================
-- STEP 5: Enable RLS on memories (the core table)
-- ============================================================
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;

-- Drop existing policy if re-running
DROP POLICY IF EXISTS tenant_isolation ON memories;

-- Tenant isolation: each tenant sees only their memories
CREATE POLICY tenant_isolation ON memories
    FOR ALL
    TO app_user
    USING (tenant_id = current_setting('app.current_tenant', TRUE)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant', TRUE)::UUID);

-- Enable RLS on backlog if it exists
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'backlog') THEN
        ALTER TABLE backlog ENABLE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS tenant_isolation ON backlog;
        CREATE POLICY tenant_isolation ON backlog
            FOR ALL
            TO app_user
            USING (tenant_id = current_setting('app.current_tenant', TRUE)::UUID)
            WITH CHECK (tenant_id = current_setting('app.current_tenant', TRUE)::UUID);
    END IF;
END $$;

-- ============================================================
-- STEP 6: Tenant lookup helper
-- ============================================================
CREATE OR REPLACE FUNCTION resolve_tenant_for_api_key(p_api_key VARCHAR)
RETURNS UUID AS $$
    SELECT t.id FROM tenants t
    JOIN api_keys ak ON ak.tenant_id = t.id
    WHERE ak.key_hash = p_api_key
      AND ak.revoked_at IS NULL
    LIMIT 1;
$$ LANGUAGE sql STABLE SECURITY DEFINER;

-- For dev/single-tenant mode: resolve default tenant when using raw API key
CREATE OR REPLACE FUNCTION resolve_tenant_default()
RETURNS UUID AS $$
    SELECT '00000000-0000-0000-0000-000000000001'::UUID;
$$ LANGUAGE sql IMMUTABLE;
