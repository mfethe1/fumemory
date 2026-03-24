-- Migration 016: Add salience_score to memories table
-- Salience controls how resistant a memory is to temporal decay.
-- High salience (0.99) = near-permanent (e.g., Sev-1 outage)
-- Low salience (0.10) = routine, decays quickly (e.g., log check)
-- Default 0.5 = neutral decay rate

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS salience_score FLOAT NOT NULL DEFAULT 0.5
    CHECK (salience_score >= 0.0 AND salience_score <= 1.0);

-- Add searchable flag for dream consolidation (Phase 3.3)
-- When false, memory retains provenance but is excluded from RAG search
ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS searchable BOOLEAN NOT NULL DEFAULT TRUE;

-- Index for efficient filtering
CREATE INDEX IF NOT EXISTS idx_memories_salience ON memories (salience_score DESC);
CREATE INDEX IF NOT EXISTS idx_memories_searchable ON memories (searchable) WHERE searchable = TRUE;

-- Add procedural memory type to the CHECK constraint
-- Must drop and recreate since ALTER CHECK is not supported
ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_memory_type_check;
ALTER TABLE memories ADD CONSTRAINT memories_memory_type_check
    CHECK (memory_type IN (
        'fact', 'decision', 'lesson', 'pattern', 'failure',
        'observation', 'reflection', 'plan', 'goal',
        'user_action', 'external', 'procedural'
    ));

