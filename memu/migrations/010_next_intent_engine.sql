-- 010_next_intent_engine.sql
-- Adds storage for proactive next-intent predictions + feedback loop.

CREATE TABLE IF NOT EXISTS intent_predictions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    signal TEXT NOT NULL,
    predicted_intent TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    horizon TEXT NOT NULL DEFAULT 'now',
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_intent_predictions_user_created
    ON intent_predictions(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS intent_feedback (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    prediction_id UUID REFERENCES intent_predictions(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    accepted BOOLEAN NOT NULL,
    actual_intent TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_intent_feedback_prediction
    ON intent_feedback(prediction_id);
