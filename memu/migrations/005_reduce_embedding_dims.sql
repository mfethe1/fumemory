# memu/migrations/005_reduce_embedding_dims.sql
-- Reduce vector dimensions to 1536 for compatibility with standard pgvector index limits (2000)
-- This assumes we switch embedding model to standard size or truncate
ALTER TABLE search_history
ALTER COLUMN embedding TYPE vector(1536);

-- Recreate index with new dimensions
DROP INDEX IF EXISTS idx_search_history_embedding;
CREATE INDEX idx_search_history_embedding ON search_history USING hnsw (embedding vector_cosine_ops);
