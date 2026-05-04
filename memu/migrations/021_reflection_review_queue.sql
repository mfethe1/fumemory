-- Migration 021: Reflection Review Queue
--
-- Adds the Reflection Review Queue and immutable action audit tables.
--
-- reflection_proposals: persists proposed Learning Memory during the 6-hour
--     review window. status tracks pending → accepted / rejected /
--     accepted_by_timeout / superseded.
-- reflection_actions: immutable audit trail for approve/deny/edit/
--     inspect/timeout_accept decisions.

-- ============================================================
-- Part 1: reflection_proposals table
-- ============================================================
CREATE TABLE IF NOT EXISTS reflection_proposals (
    proposal_id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           TEXT NOT NULL,
    status              VARCHAR(30) NOT NULL DEFAULT 'pending'
                        CHECK (status IN (
                            'pending', 'accepted', 'accepted_by_timeout',
                            'rejected', 'superseded'
                        )),
    source              VARCHAR(20) NOT NULL
                        CHECK (source IN ('task_close', 'idle_dream')),
    summary             TEXT NOT NULL,
    content             TEXT NOT NULL,
    confidence          FLOAT NOT NULL DEFAULT 0.7,
    risk_flags          JSONB NOT NULL DEFAULT '[]',
    source_task_id      TEXT,
    source_session_id   TEXT,
    source_evidence_ids JSONB NOT NULL DEFAULT '[]',
    expires_at          TIMESTAMPTZ NOT NULL,
    telegram_message_id TEXT,
    -- Set when the proposal is accepted: references the written Learning Memory
    memory_id           UUID,
    -- Set when this proposal supersedes an earlier accepted one (late feedback)
    superseded_by       UUID,
    agent_id            TEXT NOT NULL DEFAULT 'unknown',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Efficient queue processing: pending proposals by tenant + expiry window
CREATE INDEX IF NOT EXISTS idx_reflection_proposals_tenant_status
    ON reflection_proposals(tenant_id, status, expires_at);

-- Look up proposals for a specific source task
CREATE INDEX IF NOT EXISTS idx_reflection_proposals_source_task
    ON reflection_proposals(source_task_id)
    WHERE source_task_id IS NOT NULL;

-- ============================================================
-- Part 2: reflection_actions table (immutable audit trail)
-- ============================================================
CREATE TABLE IF NOT EXISTS reflection_actions (
    action_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    proposal_id     UUID NOT NULL REFERENCES reflection_proposals(proposal_id),
    actor           TEXT NOT NULL,
    decision        VARCHAR(20) NOT NULL
                    CHECK (decision IN (
                        'approve', 'deny', 'edit', 'inspect', 'timeout_accept'
                    )),
    notes           TEXT,
    edited_content  TEXT,
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reflection_actions_proposal
    ON reflection_actions(proposal_id);
