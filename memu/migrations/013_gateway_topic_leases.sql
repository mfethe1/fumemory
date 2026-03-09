-- Migration 013: topic/chat ownership leases for multi-gateway failover

CREATE TABLE IF NOT EXISTS gateway_topic_leases (
    lease_key        VARCHAR(255) PRIMARY KEY,
    owner_gateway    VARCHAR(128) NOT NULL,
    backup_gateway   VARCHAR(128),
    lease_expires_at TIMESTAMPTZ NOT NULL,
    last_message_id  VARCHAR(255),
    last_reply_id    VARCHAR(255),
    context_digest   TEXT,
    task_state       JSONB NOT NULL DEFAULT '{}',
    metadata         JSONB NOT NULL DEFAULT '{}',
    version          BIGINT NOT NULL DEFAULT 1,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gateway_topic_leases_owner ON gateway_topic_leases (owner_gateway);
CREATE INDEX IF NOT EXISTS idx_gateway_topic_leases_expiry ON gateway_topic_leases (lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_gateway_topic_leases_backup ON gateway_topic_leases (backup_gateway);

DROP TRIGGER IF EXISTS gateway_topic_leases_updated_at ON gateway_topic_leases;
CREATE TRIGGER gateway_topic_leases_updated_at
    BEFORE UPDATE ON gateway_topic_leases
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();
