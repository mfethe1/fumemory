-- Migration: Add embedding column for search_history
-- Keep at 4096 dims to match embedding provider.
ALTER TABLE search_history ADD COLUMN IF NOT EXISTS embedding vector(4096);
