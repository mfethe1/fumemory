-- Migration: Create backlog table for /tasks endpoint
-- This was referenced in api.py but never created in migrations

CREATE TABLE IF NOT EXISTS backlog (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task          TEXT NOT NULL,
    priority      VARCHAR(10) NOT NULL DEFAULT 'P2'
                  CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
    status        VARCHAR(20) NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'in_progress', 'done', 'blocked', 'cancelled')),
    owner_id      VARCHAR(64),
    lane          VARCHAR(64),
    metadata      JSONB NOT NULL DEFAULT '{}',
    evidence      TEXT,
    dependency_id UUID REFERENCES backlog(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_backlog_status ON backlog (status);
CREATE INDEX IF NOT EXISTS idx_backlog_owner ON backlog (owner_id);
CREATE INDEX IF NOT EXISTS idx_backlog_priority ON backlog (priority);

-- Add updated_at trigger
CREATE OR REPLACE TRIGGER backlog_updated_at
    BEFORE UPDATE ON backlog
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();
