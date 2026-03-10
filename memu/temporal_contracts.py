from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

TEMPORAL_ROUTE_VERSION = "2026-03-10"
DEFAULT_TEMPORAL_HOST = "localhost:7233"
DEFAULT_TEMPORAL_NAMESPACE = "default"
DEFAULT_TEMPORAL_TASK_QUEUE = "memu-queue"
DEFAULT_TEMPORAL_WORKER_PREFIX = "memu-worker"


class OrchestrationContext(BaseModel):
    """Cross-gateway context attached to Temporal requests.

    This keeps async route contracts stable across API, worker, and failover paths.
    """

    task_id: UUID | None = None
    source_gateway: str | None = Field(default=None, max_length=128)
    lease_key: str | None = Field(default=None, max_length=255)
    parent_event_id: UUID | None = None
    checkpoint_scope: str = Field(default="cross_gateway", max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def has_task_context(self) -> bool:
        return self.task_id is not None


class MemoryWorkflowInput(BaseModel):
    content: str
    agent_id: str
    metadata: dict[str, Any] | None = None
    orchestration: OrchestrationContext | None = None


class SearchWorkflowInput(BaseModel):
    query: str
    limit: int = 10
    agent_id: str | None = None
    memory_type: str | None = None
    min_confidence: float = 0.0
    temporal_weight: float = 0.3
    min_results: int = 3
    max_expansion_steps: int = 3
    lexical_fallback: bool = True
    orchestration: OrchestrationContext | None = None

    @property
    def effective_agent_id(self) -> str:
        return self.agent_id or "system"


class TemporalAcceptedResponse(BaseModel):
    status: str = "accepted"
    workflow_id: str
    task_queue: str
    namespace: str
    route_version: str = TEMPORAL_ROUTE_VERSION


class TemporalCheckpointRecord(BaseModel):
    task_id: UUID
    workflow_id: str
    gateway_id: str
    checkpoint_sequence: int = Field(..., ge=1)
    step: str = Field(..., max_length=128)
    status: str = Field(..., max_length=64)
    scratchpad: str = Field(..., max_length=100_000)
    progress_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    tokens_consumed: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TemporalTaskEventRecord(BaseModel):
    task_id: UUID
    workflow_id: str
    gateway_id: str
    event_type: str = Field(..., max_length=40)
    payload: dict[str, Any] = Field(default_factory=dict)
    parent_event_id: UUID | None = None


@dataclass(frozen=True)
class TemporalSettings:
    host: str
    namespace: str
    task_queue: str
    worker_identity: str


def load_temporal_settings(env: dict[str, str] | None = None) -> TemporalSettings:
    env = env or os.environ
    gateway_hint = (
        env.get("GATEWAY_ID")
        or env.get("GATEWAY_ROLE")
        or env.get("HOSTNAME")
        or socket.gethostname()
    )
    worker_identity = env.get(
        "TEMPORAL_WORKER_IDENTITY",
        f"{DEFAULT_TEMPORAL_WORKER_PREFIX}:{gateway_hint}:{os.getpid()}",
    )
    return TemporalSettings(
        host=env.get("TEMPORAL_HOST", DEFAULT_TEMPORAL_HOST),
        namespace=env.get("TEMPORAL_NAMESPACE", DEFAULT_TEMPORAL_NAMESPACE),
        task_queue=env.get("TEMPORAL_TASK_QUEUE", DEFAULT_TEMPORAL_TASK_QUEUE),
        worker_identity=worker_identity,
    )


def temporal_tls_enabled(host: str, env: dict[str, str] | None = None) -> bool:
    env = env or os.environ
    return env.get("TEMPORAL_TLS", "").lower() == "true" or host.endswith(":443")


def _normalize_workflow_part(part: Any) -> str:
    if part is None:
        return ""
    if isinstance(part, UUID):
        return str(part)
    if isinstance(part, dict):
        return json.dumps(part, sort_keys=True, separators=(",", ":"))
    if isinstance(part, (list, tuple, set)):
        return json.dumps(list(part), sort_keys=True, separators=(",", ":"))
    return str(part)


def canonical_workflow_id(kind: str, *parts: Any) -> str:
    payload = "|".join(_normalize_workflow_part(part) for part in parts if part is not None)
    digest = sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"{kind}-{digest}"


def coerce_orchestration_context(
    raw: OrchestrationContext | dict[str, Any] | None = None,
    *,
    metadata: dict[str, Any] | None = None,
) -> OrchestrationContext | None:
    merged: dict[str, Any] = {}
    if isinstance(metadata, dict):
        for key in ("task_id", "source_gateway", "lease_key", "parent_event_id", "checkpoint_scope"):
            if metadata.get(key) is not None:
                merged[key] = metadata[key]
        nested = metadata.get("orchestration")
        if isinstance(nested, dict):
            merged.update(nested)
    if raw is not None:
        if isinstance(raw, OrchestrationContext):
            return raw
        merged.update(raw)
    if not merged:
        return None
    return OrchestrationContext.model_validate(merged)
