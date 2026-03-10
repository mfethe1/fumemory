# memU API Reference

Canonical API routes live under `/api/v1/memu/*`.

Legacy root aliases such as `/memories`, `/search`, `/search-text`, `/chat`, `/memories/bulk`, `/memories/async`, and `/search/async` remain available for backward compatibility but are deprecated in OpenAPI.

## Authentication

Protected endpoints accept either:

- `X-API-Key: <key>` — canonical
- `Authorization: Bearer <key>` — compatibility alias

## Health Check

```text
GET /api/v1/memu/health
```

## Upsert Memory

```text
POST /api/v1/memu/upsert
```

```json
{
  "content": "text to remember",
  "agent_id": "lenny",
  "memory_type": "observation",
  "metadata": {
    "source": "telegram|slack|file",
    "agent": "agent-name",
    "tags": ["decision", "process"]
  }
}
```

## Search Memories

```text
POST /api/v1/memu/search
```

```json
{
  "query": "what did we decide?",
  "limit": 5,
  "agent_id": "mack",
  "memory_type": "decision",
  "search_strategy": "hybrid"
}
```

## Text Search

```text
GET /api/v1/memu/search-text
POST /api/v1/memu/search-text
```

## Bulk Import

```text
POST /api/v1/memu/bulk
```

## Chat (RAG)

```text
POST /api/v1/memu/chat
```

## Async / Temporal Endpoints

```text
POST /api/v1/memu/memories/async
POST /api/v1/memu/search/async
```

## Read / Delete by ID

```text
GET /api/v1/memu/{memory_id}
DELETE /api/v1/memu/{memory_id}
```

## Retrieval / Recall / Graph Endpoints

```text
GET /api/v1/memu/retrieval/status
GET /api/v1/memu/search/recall
GET /api/v1/memu/links/{memory_id}
POST /api/v1/memu/links
GET /api/v1/memu/temporal
GET /api/v1/memu/at
```

## Memory Blocks

```text
GET /api/v1/memu/blocks
GET /api/v1/memu/blocks/{key}
PUT /api/v1/memu/blocks/{key}
DELETE /api/v1/memu/blocks/{key}
```

See `memu/models.py` for request/response schemas and `openapi.json` for the generated contract.
