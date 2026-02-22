-- memU Event Sourcing Ledger — Append-Only
-- Every state change is an INSERT. No UPDATEs. No DELETEs.
-- State is reconstructed by projecting events chronologically.

CREATE TABLE IF NOT EXISTS events (
    event_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    timestamp     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    task_id       UUID NOT NULL,
    gateway_id    VARCHAR(64) NOT NULL,
    event_type    VARCHAR(40) NOT NULL
                  CHECK (event_type IN (
                      'task_drafted', 'task_amended', 'task_claimed',
                      'task_completed', 'task_failed', 'task_cancelled',
                      'bid_submitted', 'lease_granted', 'lease_expired',
                      'audit_proposed', 'audit_accepted', 'audit_rejected',
                      'circuit_breaker', 'system_halt', 'system_override',
                      'heartbeat'
                  )),
    payload       JSONB NOT NULL DEFAULT '{}',
    parent_event  UUID REFERENCES events(event_id),
    signature     VARCHAR(256),  -- future: crypto signing for zero-trust RBAC
    compute_cost  FLOAT DEFAULT 0.0  -- token cost / dollar cost for compute governor
);

-- Indexes for fast projection and audit trails
CREATE INDEX IF NOT EXISTS idx_events_task_id ON events (task_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_gateway_id ON events (gateway_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events (timestamp DESC);

-- Tasks DAG table — the directed acyclic graph of work
-- Each task is a node. Dependencies are edges via parent_task_id.
CREATE TABLE IF NOT EXISTS tasks (
    task_id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    root_prompt_id  UUID NOT NULL,  -- the original user prompt (root node of DAG)
    parent_task_id  UUID REFERENCES tasks(task_id),  -- DAG edge
    title           VARCHAR(256) NOT NULL,
    description     TEXT,
    required_capabilities JSONB DEFAULT '[]',  -- MCP capability tags needed
    status          VARCHAR(20) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'bidding', 'claimed', 'executing',
                                      'audit_pending', 'completed', 'failed', 'cancelled')),
    assigned_gateway VARCHAR(64),
    lease_expires   TIMESTAMPTZ,  -- heartbeat lease TTL
    compute_budget  FLOAT DEFAULT 0.0,  -- max tokens/dollars for this task
    compute_spent   FLOAT DEFAULT 0.0,  -- running total
    context_pointer UUID REFERENCES memories(id),  -- semantic pointer to memU memory
    dag_state       JSONB DEFAULT '{}',  -- fluid state for LangGraph
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tasks_root ON tasks (root_prompt_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks (parent_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);
CREATE INDEX IF NOT EXISTS idx_tasks_gateway ON tasks (assigned_gateway);
CREATE INDEX IF NOT EXISTS idx_tasks_lease ON tasks (lease_expires) WHERE lease_expires IS NOT NULL;

-- Root prompts — the immutable original user intents
CREATE TABLE IF NOT EXISTS root_prompts (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    content     TEXT NOT NULL,
    user_id     VARCHAR(64),
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Gateway registry — MCP capability discovery
CREATE TABLE IF NOT EXISTS gateway_registry (
    gateway_id    VARCHAR(64) PRIMARY KEY,
    capabilities  JSONB NOT NULL DEFAULT '[]',  -- MCP tool list
    status        VARCHAR(20) NOT NULL DEFAULT 'online'
                  CHECK (status IN ('online', 'offline', 'degraded')),
    last_heartbeat TIMESTAMPTZ,
    metadata      JSONB DEFAULT '{}',
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gateway_status ON gateway_registry (status);

-- Lease expiry function: auto-revoke expired leases
CREATE OR REPLACE FUNCTION revoke_expired_leases()
RETURNS INTEGER AS $$
DECLARE
    revoked_count INTEGER;
BEGIN
    WITH expired AS (
        UPDATE tasks
        SET status = 'pending',
            assigned_gateway = NULL,
            lease_expires = NULL,
            updated_at = NOW()
        WHERE status = 'claimed'
          AND lease_expires < NOW()
        RETURNING task_id, assigned_gateway
    ),
    logged AS (
        INSERT INTO events (task_id, gateway_id, event_type, payload)
        SELECT task_id, assigned_gateway, 'lease_expired',
               jsonb_build_object('reason', 'heartbeat_timeout')
        FROM expired
    )
    SELECT COUNT(*) INTO revoked_count FROM expired;

    RETURN revoked_count;
END;
$$ LANGUAGE plpgsql;
