from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import APIKeyHeader

from memu.models import MemoryCreate, SearchRequest, SearchResult
from memu.temporal_client import search_memory_workflow, store_memory_workflow

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
    """Ingest memory via Temporal workflow (async, durable)."""
    accepted = await store_memory_workflow(req)
    if accepted is None:
        raise HTTPException(status_code=503, detail="Workflow engine unavailable")
    return accepted.model_dump(mode="json")


@router.post("/search/async", response_model=list[SearchResult])
async def search_memories_async(req: SearchRequest, _key: str = Depends(verify_api_key)):
    """Search via Temporal workflow using the same request contract as /search."""
    raw_results = await search_memory_workflow(req)
    if raw_results is None:
        raise HTTPException(status_code=503, detail="Search workflow unavailable")
    return raw_results
