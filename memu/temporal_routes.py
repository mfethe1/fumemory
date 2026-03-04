# memu/temporal_routes.py
from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from memu.models import MemoryCreate, SearchRequest, SearchResult
from memu.temporal_client import store_memory_workflow, search_memory_workflow, predict_next_intent_workflow
import os

# Circular dep issue: verify_api_key is in api.py
# Redefining minimal check here to break cycle or move dep to auth.py
MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "memu-dev-key")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(key: str | None = Security(api_key_header)) -> str:
    if not key or key != MEMU_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return key

router = APIRouter()

@router.post("/memories/async")
async def create_memory_async(req: MemoryCreate, _key: str = Depends(verify_api_key)):
    """Ingest memory via Temporal workflow (Async, Durable)."""
    # Convert Pydantic to dict for json serialization in Temporal
    wf_id = await store_memory_workflow(req.content, req.agent_id, req.metadata)
    if wf_id:
        return {"status": "accepted", "workflow_id": str(wf_id)}
    else:
        raise HTTPException(status_code=503, detail="Workflow engine unavailable")

@router.post("/search/async")
async def search_memories_async(req: SearchRequest, _key: str = Depends(verify_api_key)):
    """Search via Temporal workflow (Logged, Audited)."""
    # Returns raw results list[dict]
    raw_results = await search_memory_workflow(req.query, req.agent_id or "system")
    if raw_results is None:
        raise HTTPException(status_code=503, detail="Search workflow unavailable")
    return raw_results


@router.post("/intent/predict/async")
async def predict_next_intent_async(user_id: str, signal: str, limit: int = 3, _key: str = Depends(verify_api_key)):
    """Predict likely next user intent through Temporal for durable orchestration."""
    results = await predict_next_intent_workflow(user_id=user_id, signal=signal, limit=limit)
    if results is None:
        raise HTTPException(status_code=503, detail="Intent workflow unavailable")
    return {"ok": True, "predictions": results}
