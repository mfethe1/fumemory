# memU API Reference

All endpoints require the `x-api-key` header.

## Health Check
```
GET /api/v1/memu/health
```

## Upsert Memory
```
POST /api/v1/memu/upsert
```
```json
{
  "content": "text to remember",
  "metadata": {
    "source": "telegram|slack|file",
    "agent": "agent-name",
    "tags": ["decision", "process"]
  }
}
```

## Search Memories
```
POST /api/v1/memu/search
```
```json
{
  "query": "what did we decide?",
  "k": 5,
  "filter": {"agent": "mack", "tags": ["decision"]}
}
```

## Bulk Import
```
POST /api/v1/memu/bulk
```
Import multiple memories at once from markdown files or logs.

## Chat (RAG)
```
POST /api/v1/memu/chat
```
Ask questions against your memory store with retrieval-augmented generation.

## Delete
```
DELETE /api/v1/memu/{id}
```

See `memu/models.py` for full request/response schemas.
