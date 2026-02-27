-- Migration 002: A-MEM link layer + bi-temporal columns
-- Additive only — no drops, no destructive changes.
-- Author: Macklemore | Reviewer: Lenny

-- ============================================================
-- Part 1: A-MEM Link Layer (Zettelkasten-style memory linking)
-- ============================================================

-- Bidirectional links between memories with relationship types and strength
CREATE TABLE IF NOT EXISTS memory_links (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_id     UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    target_id     UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    relationship  VARCHAR(20) NOT NULL DEFAULT 'similar'
                  CHECK (relationship IN ('similar', 'extends', 'contradicts', 'supersedes', 'caused_by', 'related')),
    strength      FLOAT NOT NULL DEFAULT 0.5 CHECK (strength >= 0 AND strength <= 1),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_accessed TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata      JSONB NOT NULL DEFAULT '{}',
    UNIQUE(source_id, target_id, relationship)
);

-- Prevent self-links
ALTER TABLE memory_links ADD CONSTRAINT no_self_links CHECK (source_id != target_id);

-- Indexes for graph traversal
CREATE INDEX IF NOT EXISTS idx_links_source ON memory_links(source_id);
CREATE INDEX IF NOT EXISTS idx_links_target ON memory_links(target_id);
CREATE INDEX IF NOT EXISTS idx_links_strength ON memory_links(strength DESC);
CREATE INDEX IF NOT EXISTS idx_links_relationship ON memory_links(relationship);

-- ============================================================
-- Part 2: Bi-Temporal Columns on memories
-- ============================================================

-- valid_from: when this version of the memory became current
-- valid_to: when this version was superseded (NULL = currently valid)
-- These enable point-in-time queries: "what did we know on Tuesday?"

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ DEFAULT NULL;

-- Index for temporal range queries
CREATE INDEX IF NOT EXISTS idx_memories_valid_range
    ON memories (valid_from, valid_to);

-- Composite index: current memories by agent
CREATE INDEX IF NOT EXISTS idx_memories_current_agent
    ON memories (agent_id, valid_from DESC)
    WHERE valid_to IS NULL;

-- ============================================================
-- Part 3: Episode Tracking (sessions → episodes → summaries)
-- ============================================================

CREATE TABLE IF NOT EXISTS episodes (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id    VARCHAR(128),
    agent_id      VARCHAR(64) NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at      TIMESTAMPTZ,
    summary       TEXT DEFAULT '',
    tags          JSONB NOT NULL DEFAULT '[]',
    memory_count  INTEGER NOT NULL DEFAULT 0,
    metadata      JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_episodes_agent ON episodes(agent_id);
CREATE INDEX IF NOT EXISTS idx_episodes_time ON episodes(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_session ON episodes(session_id);

-- Join table: which memories belong to which episodes
CREATE TABLE IF NOT EXISTS episode_memories (
    episode_id UUID NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    memory_id  UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (episode_id, memory_id)
);

CREATE INDEX IF NOT EXISTS idx_em_episode ON episode_memories(episode_id);
CREATE INDEX IF NOT EXISTS idx_em_memory ON episode_memories(memory_id);

-- ============================================================
-- Helper Views
-- ============================================================

-- Current memories only (not superseded)
CREATE OR REPLACE VIEW current_memories AS
SELECT * FROM memories WHERE valid_to IS NULL;

-- Memory with link count (for quality scoring)
CREATE OR REPLACE VIEW memory_link_stats AS
SELECT
    m.id,
    m.content,
    m.agent_id,
    m.memory_type,
    m.confidence,
    m.access_count,
    m.created_at,
    COUNT(DISTINCT ml.id) AS link_count,
    AVG(ml.strength) AS avg_link_strength
FROM memories m
LEFT JOIN memory_links ml ON (m.id = ml.source_id OR m.id = ml.target_id)
WHERE m.valid_to IS NULL
GROUP BY m.id;
