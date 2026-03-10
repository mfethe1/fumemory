-- Migration 014: Hybrid retrieval + temporal graph support
-- 1. Full-text index for BM25/ts_rank candidate generation
-- 2. Temporal graph traversal indexes for memory_links
-- 3. Keep HNSW primary vector path and add verification-friendly metadata indexes

CREATE INDEX IF NOT EXISTS idx_memories_content_fts
    ON memories USING GIN (to_tsvector('english', coalesce(content, '')));

CREATE INDEX IF NOT EXISTS idx_memory_links_source_strength
    ON memory_links (source_id, strength DESC, last_accessed DESC);

CREATE INDEX IF NOT EXISTS idx_memory_links_target_strength
    ON memory_links (target_id, strength DESC, last_accessed DESC);

CREATE INDEX IF NOT EXISTS idx_memories_created_at_desc
    ON memories (created_at DESC);
