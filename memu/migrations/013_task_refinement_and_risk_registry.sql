-- Migration: Extend backlog task registry for unified risk-aware, reviewer-driven task lifecycle

-- Core enrichment fields
ALTER TABLE backlog
    ADD COLUMN IF NOT EXISTS risk_score INTEGER NOT NULL DEFAULT 20 CHECK (risk_score BETWEEN 0 AND 100),
    ADD COLUMN IF NOT EXISTS source VARCHAR(128),
    ADD COLUMN IF NOT EXISTS source_ref VARCHAR(500),
    ADD COLUMN IF NOT EXISTS project VARCHAR(128),
    ADD COLUMN IF NOT EXISTS completion_criteria TEXT,
    ADD COLUMN IF NOT EXISTS review_status VARCHAR(20) NOT NULL DEFAULT 'pending_review' CHECK (review_status IN ('pending_review', 'passed', 'needs_input', 'blocked', 'completed')),
    ADD COLUMN IF NOT EXISTS reviewer_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS review_notes TEXT,
    ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS refine_status VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (refine_status IN ('pending', 'approved', 'rejected', 'needs_revision')),
    ADD COLUMN IF NOT EXISTS refined_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS source_fingerprint VARCHAR(64),
    ADD COLUMN IF NOT EXISTS menu_bucket VARCHAR(64);

-- Ensure status now includes the full lifecycle needed by the unified registry.
DO $$
DECLARE
    rec RECORD;
BEGIN
    FOR rec IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'backlog'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE format('ALTER TABLE backlog DROP CONSTRAINT %I', rec.conname);
    END LOOP;

    ALTER TABLE backlog
        ADD CONSTRAINT backlog_status_check
        CHECK (status IN ('pending','claimed','in_progress','done','blocked','cancelled'));
EXCEPTION
    WHEN duplicate_object THEN
        -- Constraint already exists with this exact signature.
        NULL;
END $$;

-- Helpful indexes for high-volume task scans
CREATE INDEX IF NOT EXISTS idx_backlog_risk_score ON backlog (risk_score DESC);
CREATE INDEX IF NOT EXISTS idx_backlog_source ON backlog (source);
CREATE INDEX IF NOT EXISTS idx_backlog_project ON backlog (project);
CREATE INDEX IF NOT EXISTS idx_backlog_review_status ON backlog (review_status);
CREATE INDEX IF NOT EXISTS idx_backlog_refine_status ON backlog (refine_status);
CREATE INDEX IF NOT EXISTS idx_backlog_fingerprint ON backlog (source_fingerprint);
CREATE INDEX IF NOT EXISTS idx_backlog_menu_bucket ON backlog (menu_bucket);

-- Keep backward compatible status model, but normalize in-progress labels
UPDATE backlog
SET status = 'in_progress'
WHERE status = 'claimed';