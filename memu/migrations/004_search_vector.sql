-- Migration: Add embedding column for vector search
ALTER TABLE search_history ADD COLUMN IF NOT EXISTS embedding vector(4096);
CREATE INDEX IF NOT EXISTS idx_search_history_embedding ON search_history USING hnsw (embedding vector_cosine_ops);
