-- Day 6: Tuned HNSW index for approximate nearest-neighbor search on memory embeddings.
-- Uses cosine distance (vector_cosine_ops) to match the application's similarity metric.
-- Parameters: m=16 (connections per layer), ef_construction=64 (build-time accuracy).
--
-- The original index (000_schema.sql) was created without tuning parameters.
-- Drop and re-create with explicit m/ef_construction for predictable query performance
-- at scale (>50k rows).

DROP INDEX IF EXISTS idx_memories_embedding;

CREATE INDEX idx_memories_embedding_hnsw
ON memories
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

