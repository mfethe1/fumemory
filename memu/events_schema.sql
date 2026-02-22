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
                      'heartbeat',
                      'lane_acquired', 'lane_released', 'lane_contested',
                      'task_orphaned', 'task_hydrated', 'checkpoint_saved',
                      'rollback_executed',
                      'dlq_enqueued', 'dlq_diagnosed', 'dlq_healed',
                      'rpc_request', 'rpc_response'
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
                                      'audit_pending', 'completed', 'failed', 'cancelled',
                                      'orphaned', 'hydrating', 'yielding', 'dlq', 'rolling_back')),
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

-- Lane locks table — semantic mutexes with fencing tokens
CREATE TABLE IF NOT EXISTS lane_locks (
    lane_id         VARCHAR(256) PRIMARY KEY,
    gateway_id      VARCHAR(64) NOT NULL,
    task_id         UUID NOT NULL REFERENCES tasks(task_id),
    fencing_token   INTEGER NOT NULL,
    acquired_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    CONSTRAINT positive_fencing CHECK (fencing_token > 0)
);

CREATE INDEX IF NOT EXISTS idx_lane_locks_gateway ON lane_locks (gateway_id);
CREATE INDEX IF NOT EXISTS idx_lane_locks_expires ON lane_locks (expires_at);

-- Fencing token sequence — monotonically increasing per lane
CREATE TABLE IF NOT EXISTS fencing_tokens (
    lane_id     VARCHAR(256) PRIMARY KEY,
    token       INTEGER NOT NULL DEFAULT 0
);

-- Dead Letter Queue — poison pill tasks
CREATE TABLE IF NOT EXISTS dead_letter_queue (
    task_id             UUID PRIMARY KEY REFERENCES tasks(task_id),
    failure_count       INTEGER NOT NULL CHECK (failure_count >= 3),
    failures            JSONB NOT NULL DEFAULT '[]',
    root_cause_diagnosis TEXT,
    proposed_amendment  JSONB,
    entered_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_dlq_unresolved ON dead_letter_queue (entered_at) WHERE resolved_at IS NULL;

-- Checkpoints — incremental scratchpad state for hydration
CREATE TABLE IF NOT EXISTS checkpoints (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id             UUID NOT NULL REFERENCES tasks(task_id),
    gateway_id          VARCHAR(64) NOT NULL,
    checkpoint_seq      INTEGER NOT NULL,
    scratchpad          TEXT NOT NULL,
    progress_pct        FLOAT DEFAULT 0.0,
    tokens_consumed     INTEGER DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (task_id, checkpoint_seq)
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON checkpoints (task_id, checkpoint_seq DESC);

-- Epistemic Blackboard — shared knowledge across all gateways
CREATE TABLE IF NOT EXISTS blackboard (
    entry_id        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    key             VARCHAR(256) NOT NULL,
    value           TEXT NOT NULL,
    category        VARCHAR(20) NOT NULL DEFAULT 'fact'
                    CHECK (category IN ('fact', 'rule', 'warning', 'capability')),
    source_gateway  VARCHAR(64) NOT NULL,
    confidence      FLOAT NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    supersedes      UUID REFERENCES blackboard(entry_id),
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_until     TIMESTAMPTZ,
    access_count    INTEGER NOT NULL DEFAULT 0,
    -- Hallucination Contagion Prevention (Lenny's Validation Gate)
    is_validated    BOOLEAN NOT NULL DEFAULT FALSE,
    validated_by    VARCHAR(64),
    validated_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_blackboard_key ON blackboard (key);
CREATE INDEX IF NOT EXISTS idx_blackboard_category ON blackboard (category);
CREATE INDEX IF NOT EXISTS idx_blackboard_active ON blackboard (valid_from DESC)
    WHERE valid_until IS NULL OR valid_until > NOW();

-- Container registry — Warden tracks all containers
CREATE TABLE IF NOT EXISTS container_registry (
    container_id    VARCHAR(128) PRIMARY KEY,
    gateway_id      VARCHAR(64) NOT NULL,
    role            VARCHAR(20) NOT NULL
                    CHECK (role IN ('gateway', 'sandbox', 'warm_standby')),
    state           VARCHAR(20) NOT NULL DEFAULT 'booting'
                    CHECK (state IN ('booting', 'hydrating', 'active', 'idle', 'draining', 'dead')),
    task_id         UUID,
    fencing_token   INTEGER,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_heartbeat  TIMESTAMPTZ,
    crash_count     INTEGER NOT NULL DEFAULT 0,
    metadata        JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_containers_state ON container_registry (state);
CREATE INDEX IF NOT EXISTS idx_containers_gateway ON container_registry (gateway_id);

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
