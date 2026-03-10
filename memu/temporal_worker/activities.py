from __future__ import annotations

import json
import os
from typing import Any

import asyncpg
import httpx
from temporalio import activity

from memu.decay import compute_final_score
from memu.temporal_contracts import TemporalCheckpointRecord, TemporalTaskEventRecord

# Local cache for FastEmbed fallback (kept module-private for worker determinism)
_fastembed_model: Any | None = None


def get_db_url():
    return os.environ.get("DATABASE_URL")


# --- Embedding helpers (OpenAI-compatible + local fallback) ---

EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
EMBEDDING_DIMS = int(os.environ.get("EMBEDDING_DIMS", "384"))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
DECAY_RATE = float(os.environ.get("DECAY_RATE", "0.01"))


async def _embedding_from_http(text: str) -> list[float] | None:
    """Try OpenAI-compatible endpoint (/v1/embeddings), including Ollama compatibility."""
    if not OPENAI_API_KEY and "ollama" not in EMBEDDING_BASE_URL:
        return None

    base = EMBEDDING_BASE_URL.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if OPENAI_API_KEY:
        headers["Authorization"] = f"Bearer {OPENAI_API_KEY}"

    async def _try(url: str, payload: dict) -> list[float] | None:
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if "data" in data and data["data"]:
                    emb = data["data"][0].get("embedding")
                    if emb is not None:
                        return emb
                if "embedding" in data:
                    return data["embedding"]
        except Exception as exc:
            activity.logger.warning("Remote embedding request failed (%s): %s", url, exc)
        return None

    emb = await _try(
        f"{base}/v1/embeddings",
        {
            "input": text,
            "model": EMBEDDING_MODEL,
            "dimensions": EMBEDDING_DIMS,
        },
    )
    if emb is not None and len(emb) == EMBEDDING_DIMS:
        return emb

    emb = await _try(
        f"{base}/api/embeddings",
        {
            "model": EMBEDDING_MODEL,
            "prompt": text,
        },
    )
    if emb is not None and len(emb) == EMBEDDING_DIMS:
        return emb
    if emb is not None:
        activity.logger.warning(
            "Embedding dim mismatch from remote provider: got=%d expected=%d",
            len(emb),
            EMBEDDING_DIMS,
        )

    return None


async def _embedding_from_fastembed(text: str) -> list[float] | None:
    global _fastembed_model

    if _fastembed_model is None:
        try:
            from fastembed import TextEmbedding

            _fastembed_model = TextEmbedding()
        except Exception as exc:
            activity.logger.warning("FastEmbed unavailable: %s", exc)
            return None

    try:
        embeddings = list(_fastembed_model.embed([text]))
        emb = embeddings[0].tolist()
        if len(emb) == EMBEDDING_DIMS:
            return emb
        activity.logger.warning(
            "FastEmbed dim mismatch: got=%d expected=%d",
            len(emb),
            EMBEDDING_DIMS,
        )
    except Exception as exc:
        activity.logger.warning("FastEmbed generation failed: %s", exc)
    return None


async def _insert_event(conn: asyncpg.Connection, event: TemporalTaskEventRecord) -> bool:
    try:
        await conn.execute(
            """
            INSERT INTO events (task_id, gateway_id, event_type, payload, parent_event)
            VALUES ($1, $2, $3, $4::jsonb, $5)
            """,
            event.task_id,
            event.gateway_id,
            event.event_type,
            json.dumps(event.payload),
            event.parent_event_id,
        )
        return True
    except Exception as exc:
        activity.logger.warning("Failed to persist Temporal event %s: %s", event.event_type, exc)
        return False


@activity.defn
async def generate_embedding(text: str) -> list[float] | None:
    """Generate embedding vector (activity wrapper)."""
    remote = await _embedding_from_http(text)
    if remote is not None:
        return remote
    return await _embedding_from_fastembed(text)


@activity.defn
async def store_memory(content: str, agent_id: str, metadata: dict | None, embedding: list[float] | None) -> str:
    """Store memory and return ID."""
    conn = await asyncpg.connect(get_db_url())
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO memories (content, agent_id, metadata, memory_type, embedding)
            VALUES ($1, $2, $3, 'user_action', $4::vector)
            RETURNING id
            """,
            content,
            agent_id,
            json.dumps(metadata or {}),
            str(embedding) if embedding else None,
        )
        return str(row["id"])
    finally:
        await conn.close()


@activity.defn
async def search_memory(request: dict[str, Any], embedding: list[float] | None) -> list[dict[str, Any]]:
    """Execute async search while preserving the SearchRequest contract."""
    query = request["query"]
    agent_id = request.get("agent_id") or "system"
    limit = int(request.get("limit") or 10)
    memory_type = request.get("memory_type")
    min_confidence = float(request.get("min_confidence") or 0.0)
    temporal_weight = float(request.get("temporal_weight") or 0.3)

    conn = await asyncpg.connect(get_db_url())
    try:
        params: list[Any] = []
        filters: list[str] = []
        idx = 1

        if embedding is not None:
            vec = f"vector({EMBEDDING_DIMS})"
            params.append(str(embedding))
            idx += 1
            similarity_select = f"1 - (embedding <=> $1::{vec}) AS similarity"
            order_clause = f"ORDER BY embedding <=> $1::{vec}"
            filters.append("embedding IS NOT NULL")
        else:
            similarity_select = "0.0::float8 AS similarity"
            params.append(f"%{query}%")
            order_clause = "ORDER BY updated_at DESC"
            filters.append("content ILIKE $1")
            idx += 1

        filters.append(f"agent_id = ${idx}")
        params.append(agent_id)
        idx += 1

        if memory_type:
            filters.append(f"memory_type = ${idx}")
            params.append(memory_type)
            idx += 1
        if min_confidence > 0:
            filters.append(f"confidence >= ${idx}")
            params.append(min_confidence)
            idx += 1

        rows = await conn.fetch(
            f"""
            SELECT *, {similarity_select}
            FROM memories
            WHERE {' AND '.join(filters)}
            {order_clause}
            LIMIT {limit}
            """,
            *params,
        )

        results: list[dict[str, Any]] = []
        for row in rows:
            metadata = row["metadata"]
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            similarity = float(row["similarity"])
            final_score = compute_final_score(
                similarity=similarity if embedding is not None else 0.5,
                created_at=row["created_at"],
                access_count=row["access_count"],
                decay_rate=DECAY_RATE,
                temporal_weight=temporal_weight,
            )
            results.append(
                {
                    "memory": {
                        "id": row["id"],
                        "content": row["content"],
                        "memory_type": row["memory_type"],
                        "agent_id": row["agent_id"],
                        "metadata": metadata,
                        "parent_id": row["parent_id"],
                        "confidence": row["confidence"],
                        "access_count": row["access_count"],
                        "decay_score": row["decay_score"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    },
                    "similarity": similarity,
                    "final_score": final_score,
                }
            )
        return results
    finally:
        await conn.close()


@activity.defn
async def log_audit(action_type: str, agent_id: str, details: dict):
    """Log structured audit event."""
    conn = await asyncpg.connect(get_db_url())
    try:
        await conn.execute(
            """
            INSERT INTO audit_log (action_type, agent_id, details, created_at)
            VALUES ($1, $2, $3, NOW())
            """,
            action_type,
            agent_id,
            json.dumps(details),
        )
        return True
    finally:
        await conn.close()


@activity.defn
async def record_temporal_checkpoint(record: dict[str, Any]) -> bool:
    payload = TemporalCheckpointRecord.model_validate(record)
    conn = await asyncpg.connect(get_db_url())
    try:
        await conn.execute(
            """
            INSERT INTO checkpoints (
                task_id, gateway_id, checkpoint_seq, scratchpad, progress_pct, tokens_consumed
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (task_id, checkpoint_seq)
            DO UPDATE SET
                scratchpad = EXCLUDED.scratchpad,
                progress_pct = EXCLUDED.progress_pct,
                tokens_consumed = EXCLUDED.tokens_consumed,
                gateway_id = EXCLUDED.gateway_id
            """,
            payload.task_id,
            payload.gateway_id,
            payload.checkpoint_sequence,
            payload.scratchpad,
            payload.progress_pct,
            payload.tokens_consumed,
        )
        event = TemporalTaskEventRecord(
            task_id=payload.task_id,
            workflow_id=payload.workflow_id,
            gateway_id=payload.gateway_id,
            event_type="checkpoint_saved",
            payload={
                "workflow_id": payload.workflow_id,
                "step": payload.step,
                "status": payload.status,
                "checkpoint_sequence": payload.checkpoint_sequence,
                "progress_pct": payload.progress_pct,
                "tokens_consumed": payload.tokens_consumed,
                "metadata": payload.metadata,
            },
        )
        return await _insert_event(conn, event)
    finally:
        await conn.close()


@activity.defn
async def record_task_event(record: dict[str, Any]) -> bool:
    event = TemporalTaskEventRecord.model_validate(record)
    conn = await asyncpg.connect(get_db_url())
    try:
        return await _insert_event(conn, event)
    finally:
        await conn.close()


# Export for worker registration
GenerateEmbeddingActivity = generate_embedding
StoreMemoryActivity = store_memory
SearchMemoryActivity = search_memory
LogAuditActivity = log_audit
RecordTemporalCheckpointActivity = record_temporal_checkpoint
RecordTaskEventActivity = record_task_event
