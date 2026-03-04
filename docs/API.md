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

## Next Intent Prediction
```
POST /api/v1/intent/predict
```
```json
{
  "user_id": "michael",
  "signal": "status of railway and nats",
  "limit": 3
}
```

Returns ranked likely next intents with confidence + evidence and stores them for learning.

## Proactive Drafts
```
POST /api/v1/intent/proactive-draft
```
Same request schema as predict; returns reversible action drafts (prep plans/checklists) per predicted intent.

## Intent Feedback
```
POST /api/v1/intent/feedback
```
```json
{
  "prediction_id": "<uuid>",
  "user_id": "michael",
  "accepted": true,
  "actual_intent": "remediation",
  "notes": "good prediction"
}
```

## Delete
```
DELETE /api/v1/memu/{id}
```

See `memu/models.py` for full request/response schemas.
