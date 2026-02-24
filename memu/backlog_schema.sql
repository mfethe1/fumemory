-- Backlog & Coordination Schema Extension

CREATE TABLE IF NOT EXISTS backlog (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task          TEXT NOT NULL,
    priority      VARCHAR(10) DEFAULT 'P2' CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
    status        VARCHAR(20) DEFAULT 'todo' CHECK (status IN ('todo', 'in-progress', 'done', 'blocked')),
    owner_id      VARCHAR(64),
    lane          VARCHAR(64) DEFAULT 'general',
    metadata      JSONB NOT NULL DEFAULT '{}',
    evidence      TEXT,
    dependency_id UUID REFERENCES backlog(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sync_state (
    file_path     TEXT PRIMARY KEY,
    last_hash     VARCHAR(64),
    last_synced   TIMESTAMPTZ DEFAULT NOW(),
    status        VARCHAR(20) DEFAULT 'synced'
);

CREATE INDEX IF NOT EXISTS idx_backlog_status ON backlog (status);
CREATE INDEX IF NOT EXISTS idx_backlog_priority ON backlog (priority);
CREATE INDEX IF NOT EXISTS idx_backlog_owner ON backlog (owner_id);

-- Update trigger for backlog
CREATE OR REPLACE TRIGGER backlog_updated_at
    BEFORE UPDATE ON backlog
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();
