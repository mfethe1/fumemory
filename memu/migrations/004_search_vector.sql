-- Migration: Add embedding column for vector search
ALTER TABLE search_history ADD COLUMN IF NOT EXISTS embedding vector(4096);
-- HNSW supports max 2000 dims. For 4096, use IVFFlat.
CREATE INDEX IF NOT EXISTS idx_search_history_embedding ON search_history USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
