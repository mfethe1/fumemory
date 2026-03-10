# memu/temporal_routes.py
from fastapi import APIRouter, Depends, HTTPException

from memu.auth import verify_api_key
from memu.models import MemoryCreate, SearchRequest
from memu.temporal_client import search_memory_workflow, store_memory_workflow

router = APIRouter()

@router.post("/api/v1/memu/memories/async", tags=["Async Workflows"])
@router.post("/memories/async", deprecated=True, tags=["Async Workflows"])
async def create_memory_async(req: MemoryCreate, _key: str = Depends(verify_api_key)):
    """Ingest memory via Temporal workflow (Async, Durable)."""
    # Convert Pydantic to dict for json serialization in Temporal
    wf_id = await store_memory_workflow(req.content, req.agent_id, req.metadata)
    if wf_id:
        return {"status": "accepted", "workflow_id": str(wf_id)}
    else:
        raise HTTPException(status_code=503, detail="Workflow engine unavailable")

@router.post("/api/v1/memu/search/async", tags=["Async Workflows"])
@router.post("/search/async", deprecated=True, tags=["Async Workflows"])
async def search_memories_async(req: SearchRequest, _key: str = Depends(verify_api_key)):
    """Search via Temporal workflow (Logged, Audited)."""
    # Returns raw results list[dict]
    raw_results = await search_memory_workflow(req.query, req.agent_id or "system")
    if raw_results is None:
        raise HTTPException(status_code=503, detail="Search workflow unavailable")
    return raw_results
