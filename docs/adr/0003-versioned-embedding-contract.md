# Use a Versioned Embedding Contract

fumemory standardizes embedding configuration on `EMBEDDING_API_BASE`, `EMBEDDING_MODEL`, and `EMBEDDING_DIMS`; `EMBEDDING_BASE_URL` is a temporary compatibility alias only. Production defaults to an OpenAI-compatible `text-embedding-3-small` / `1536` contract unless a hosted Ollama embedding service is explicitly selected.

Embedding dimension changes must be additive and versioned, using a new vector column, table, or embedding version plus background reindex. Destructive migrations that drop and recreate production embedding columns are rejected because they erase searchable memory and make Railway deploys unsafe.
