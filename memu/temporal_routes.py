# memu/temporal_routes.py
from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from memu.models import MemoryCreate, SearchRequest
from memu.temporal_client import store_memory_workflow, search_memory_workflow
import os

# Circular dep issue: verify_api_key is in api.py
# Redefining minimal check here to break cycle or move dep to auth.py
MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "memu-dev-key")
memu_key_header = APIKeyHeader(name="X-MemU-Key", auto_error=False)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(
    memu_key: str | None = Security(memu_key_header),
    legacy_key: str | None = Security(api_key_header),
) -> str:
    key = memu_key or legacy_key
    if not key or key != MEMU_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return key

router = APIRouter()

def _req_to_dict(req: MemoryCreate) -> dict:
    """Serialize MemoryCreate to a plain dict for Temporal workflow args.

    All canonical evidence fields are preserved so the async path never
    silently drops memory_type, memory_kind, idempotency_key, or provenance.
    """
    metadata: dict = dict(req.metadata or {})
    metadata["allowed_roles"] = req.allowed_roles or ["*"]
    return {
        "content": req.content,
        "agent_id": req.agent_id,
        "memory_type": req.memory_type.value,
        "memory_kind": req.memory_kind.value,
        "idempotency_key": req.idempotency_key,
        "salience_score": req.salience_score,
        "allowed_roles": req.allowed_roles or ["*"],
        "metadata": metadata,
        "parent_id": str(req.parent_id) if req.parent_id else None,
        # async path uses default tenant; multi-tenant async is a follow-up
        "tenant_id": "00000000-0000-0000-0000-000000000001",
    }

@router.post("/memories/async")
async def create_memory_async(req: MemoryCreate, _key: str = Depends(verify_api_key)):
    """Ingest memory via Temporal workflow (Async, Durable).

    When Temporal is unavailable the endpoint returns an explicit degraded
    status rather than a generic error, so callers can distinguish "workflow
    engine down" from other failures.  Missing Temporal does NOT affect Core
    API Readiness — only the optional async gate.
    """
    req_dict = _req_to_dict(req)
    wf_id = await store_memory_workflow(req_dict)
    if wf_id:
        return {"status": "accepted", "workflow_id": str(wf_id)}
    raise HTTPException(
        status_code=503,
        detail={
            "status": "degraded",
            "reason": "temporal_unavailable",
            "message": (
                "Temporal workflow engine is not available. "
                "Async memory writes require Temporal. "
                "Core API Readiness is not affected."
            ),
        },
    )

@router.post("/search/async")
async def search_memories_async(req: SearchRequest, _key: str = Depends(verify_api_key)):
    """Search via Temporal workflow (Logged, Audited)."""
    # Returns raw results list[dict]
    raw_results = await search_memory_workflow(req.query, req.agent_id or "system")
    if raw_results is None:
        raise HTTPException(status_code=503, detail="Search workflow unavailable")
    return raw_results
