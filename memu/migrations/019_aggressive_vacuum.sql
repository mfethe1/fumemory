-- Migration 019: Aggressive Autovacuum for Bitemporal Memory Table
--
-- Problem: Bitemporal updates (valid_to writes, superseding operations) generate
-- millions of dead tuples. PostgreSQL's default autovacuum settings
-- (scale_factor=0.2, threshold=50) are far too conservative for high-churn
-- tables, causing HNSW index bloat and degraded ANN query latency.
--
-- Solution: Force aggressive garbage collection specifically on the memories table.
--   - autovacuum_vacuum_scale_factor = 0.02 (vacuum at 2% dead tuples vs default 20%)
--   - autovacuum_vacuum_threshold = 500 (vacuum after 500 dead tuples vs default 50)
--
-- This ensures PostgreSQL cleans up dead AI thoughts almost immediately,
-- preventing MVCC bloat from degrading the Semantic Router.

ALTER TABLE memories SET (
    autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_vacuum_threshold = 500
);

