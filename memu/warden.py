"""The Warden — Infrastructure Control Plane.

Non-LLM microservice that manages Docker container lifecycle.
This is the ONLY module allowed to import the Docker SDK.

Responsibilities:
- Respawn crashed gateways from signed NATS events
- Maintain warm standby pool for instant failover
- Enforce MAX_CONTAINERS hard cap (fork bomb prevention)
- Spin up ephemeral sandboxes for untrusted code execution
- Monitor heartbeats and trigger Dead Man's Switch
- Adaptive warm pool sizing based on task queue depth
- Dual-deploy awareness (local primary + Railway fallback)

Security:
- Validates cryptographic signatures on all spawn requests
- Revokes fencing tokens before spawning replacements
- Air-gapped sandbox network (--network none)
- No host volume mounts to gateway containers
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# --- Configuration ---

MAX_CONTAINERS = int(os.environ.get("WARDEN_MAX_CONTAINERS", "10"))
MIN_WARM_POOL = int(os.environ.get("WARDEN_MIN_WARM_POOL", "0"))  # 0 = cold start mode (Lenny's fix for low-RAM hosts)
MAX_WARM_POOL = int(os.environ.get("WARDEN_MAX_WARM_POOL", "3"))
WARM_POOL_MODE = os.environ.get("WARDEN_WARM_POOL_MODE", "cold")  # "cold" | "warm" — cold for <8GB RAM hosts
HEARTBEAT_TTL_S = int(os.environ.get("WARDEN_HEARTBEAT_TTL", "10"))
HEARTBEAT_CHECK_INTERVAL_S = float(os.environ.get("WARDEN_CHECK_INTERVAL", "3.0"))
SANDBOX_TIMEOUT_S = int(os.environ.get("WARDEN_SANDBOX_TIMEOUT", "30"))
MAX_HYDRATION_TOKENS = int(os.environ.get("WARDEN_MAX_HYDRATION_TOKENS", "8000"))
WARDEN_SECRET = os.environ.get("WARDEN_SECRET", "")
if not WARDEN_SECRET:
    logger.warning(
        "WARDEN_SECRET not set — respawn request signatures will be rejected. "
        "Set WARDEN_SECRET env var in production."
    )
CRASH_LOOP_THRESHOLD = 3  # DLQ after this many crashes

# Base Docker image for gateways
GATEWAY_IMAGE = os.environ.get("WARDEN_GATEWAY_IMAGE", "fumemory-gateway:latest")
SANDBOX_IMAGE = os.environ.get("WARDEN_SANDBOX_IMAGE", "python:3.11-alpine")


# --- Enums ---

class ContainerRole(str, Enum):
    GATEWAY = "gateway"
    SANDBOX = "sandbox"
    WARM_STANDBY = "warm_standby"


class ContainerState(str, Enum):
    BOOTING = "booting"
    HYDRATING = "hydrating"
    ACTIVE = "active"
    IDLE = "idle"          # Warm pool
    DRAINING = "draining"
    DEAD = "dead"


class WardenEventType(str, Enum):
    RESPAWN_REQUEST = "respawn_request"
    SPAWN_SANDBOX = "spawn_sandbox"
    CONTAINER_STARTED = "container_started"
    CONTAINER_DIED = "container_died"
    CONTAINER_KILLED = "container_killed"
    HEARTBEAT_EXPIRED = "heartbeat_expired"
    WARM_POOL_SCALED = "warm_pool_scaled"
    FORK_BOMB_BLOCKED = "fork_bomb_blocked"
    SANDBOX_COMPLETED = "sandbox_completed"
    CRASH_LOOP_DETECTED = "crash_loop_detected"
    FENCING_TOKEN_REVOKED = "fencing_token_revoked"


# --- Pydantic Models (strict schemas for all Pub/Sub messages) ---

class RespawnRequest(BaseModel):
    """Signed request to respawn a crashed gateway."""
    request_id: UUID = Field(default_factory=uuid4)
    target_role: str = Field(..., max_length=64, description="Gateway role to respawn")
    dead_gateway_id: str = Field(..., max_length=64)
    task_id: UUID | None = None
    reason: str = Field(..., max_length=500)
    requesting_gateway: str = Field(..., max_length=64)
    signature: str = Field(..., description="HMAC-SHA256 signature for validation")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def verify_signature(self, secret: str) -> bool:
        """Verify the HMAC signature against the request payload."""
        payload = f"{self.request_id}:{self.target_role}:{self.dead_gateway_id}:{self.timestamp.isoformat()}"
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(self.signature, expected)

    @staticmethod
    def sign(request_id: UUID, target_role: str, dead_gateway_id: str,
             timestamp: datetime, secret: str) -> str:
        """Generate HMAC signature for a respawn request."""
        payload = f"{request_id}:{target_role}:{dead_gateway_id}:{timestamp.isoformat()}"
        return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


class SandboxRequest(BaseModel):
    """Request to execute untrusted code in an air-gapped sandbox."""
    request_id: UUID = Field(default_factory=uuid4)
    requesting_gateway: str = Field(..., max_length=64)
    task_id: UUID
    code: str = Field(..., max_length=100_000)
    language: str = Field("python", description="python | bash | node")
    timeout_s: int = Field(SANDBOX_TIMEOUT_S, ge=1, le=300)
    signature: str


class SandboxResult(BaseModel):
    """Result from an ephemeral sandbox execution."""
    request_id: UUID
    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    execution_ms: int = 0
    container_id: str | None = None


class ContainerInfo(BaseModel):
    """Container state for the Glass Box UI."""
    container_id: str
    gateway_id: str
    role: ContainerRole
    state: ContainerState
    task_id: UUID | None = None
    started_at: datetime
    last_heartbeat: datetime | None = None
    crash_count: int = 0
    fencing_token: int | None = None
    image: str = GATEWAY_IMAGE
    memory_mb: float = 0.0
    cpu_pct: float = 0.0


class WarmPoolStatus(BaseModel):
    """Warm pool state for monitoring."""
    target_size: int
    current_size: int
    pending_tasks: int
    active_gateways: int
    scaling_action: str | None = None  # "scale_up" | "scale_down" | None


class WardenStatus(BaseModel):
    """Full Warden status for the Glass Box UI."""
    active_containers: list[ContainerInfo] = Field(default_factory=list)
    warm_pool: WarmPoolStatus
    total_containers: int = 0
    max_containers: int = MAX_CONTAINERS
    crash_loops: list[dict[str, Any]] = Field(default_factory=list)
    uptime_s: float = 0.0
    last_respawn: datetime | None = None


# --- Blackboard (Epistemic Shared State) ---

class BlackboardEntry(BaseModel):
    """A single insight on the Epistemic Blackboard.

    When a gateway discovers something systemic (e.g., 'API X requires Bearer auth'),
    it publishes here. All gateways instantly receive it.

    IMPORTANT (Lenny's Refinement): Entries start as UNTRUSTED (is_validated=False).
    A second independent gateway or the Medic must verify before the swarm trusts it.
    This prevents "Hallucination Contagion" — one gateway's hallucinated fact
    infecting the entire swarm.
    """
    entry_id: UUID = Field(default_factory=uuid4)
    key: str = Field(..., max_length=256, description="Namespaced key (e.g. 'api.github.auth_type')")
    value: str = Field(..., max_length=10_000)
    category: str = Field("fact", description="fact | rule | warning | capability")
    source_gateway: str = Field(..., max_length=64)
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    supersedes: UUID | None = Field(None, description="Entry this replaces (temporal correctness)")
    valid_from: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    valid_until: datetime | None = None
    access_count: int = 0

    # Hallucination Contagion Prevention (Lenny's Validation Gate)
    is_validated: bool = Field(
        False,
        description="UNTRUSTED until a second independent gateway verifies this insight"
    )
    validated_by: str | None = Field(
        None, max_length=64,
        description="Gateway ID that independently verified this entry"
    )
    validated_at: datetime | None = None

    @property
    def is_expired(self) -> bool:
        if self.valid_until is None:
            return False
        return datetime.now(timezone.utc) > self.valid_until

    @property
    def is_trusted(self) -> bool:
        """An entry is trusted only when validated by a second source and not expired."""
        return self.is_validated and not self.is_expired


class BlackboardSnapshot(BaseModel):
    """Full blackboard state — downloaded by new replicas on boot."""
    entries: list[BlackboardEntry] = Field(default_factory=list)
    version: int = 0
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def active_entries(self) -> list[BlackboardEntry]:
        """Only non-expired, non-superseded entries."""
        superseded_ids = {e.supersedes for e in self.entries if e.supersedes}
        return [
            e for e in self.entries
            if not e.is_expired and e.entry_id not in superseded_ids
        ]

    @property
    def trusted_entries(self) -> list[BlackboardEntry]:
        """Only validated, non-expired, non-superseded entries.
        Unvalidated entries are visible but marked as untrusted."""
        return [e for e in self.active_entries if e.is_trusted]

    def to_context_string(self, max_tokens: int = 4000, include_untrusted: bool = False) -> str:
        """Render active entries as a context string for LLM injection.

        By default, only injects VALIDATED entries. Untrusted entries
        are excluded from the LLM context to prevent hallucination contagion.
        Set include_untrusted=True to see (but mark) unverified insights.
        """
        if include_untrusted:
            entries = self.active_entries
        else:
            entries = self.trusted_entries

        sorted_entries = sorted(
            entries,
            key=lambda e: (e.confidence, e.valid_from.timestamp()),
            reverse=True,
        )

        lines = ["## Shared Knowledge (Epistemic Blackboard)"]
        char_budget = max_tokens * 4  # rough chars-per-token

        for entry in sorted_entries:
            trust_tag = "✓" if entry.is_validated else "⚠ UNVERIFIED"
            line = f"- [{entry.category}] [{trust_tag}] {entry.key}: {entry.value}"
            if len("\n".join(lines)) + len(line) > char_budget:
                lines.append(f"... ({len(sorted_entries) - len(lines) + 1} more entries truncated)")
                break
            lines.append(line)

        return "\n".join(lines)


# --- Hydration Config ---

class HydrationConfig(BaseModel):
    """Configuration for boot-time context hydration.

    Controls how much state a fresh container downloads from memU.
    """
    max_tokens: int = Field(MAX_HYDRATION_TOKENS, ge=1000, le=32000)
    include_blackboard: bool = True
    include_checkpoint: bool = True
    include_dag_context: bool = True
    summarize_if_over_budget: bool = True
    summarization_model: str = "gpt-4o-mini"


# --- NATS Subjects ---

WARDEN_SUBJECTS = {
    "respawn": "swarm.warden.respawn",
    "sandbox": "swarm.warden.sandbox",
    "sandbox_result": "swarm.warden.sandbox.result",
    "heartbeat": "swarm.warden.heartbeat",
    "status": "swarm.warden.status",
    "blackboard": "swarm.blackboard",
    "blackboard_sync": "swarm.blackboard.sync",
    "blackboard_validate": "swarm.blackboard.validate",
    "container_event": "swarm.warden.container",
    "suicide": "swarm.advisory.suicide",  # Lenny's split-brain fix — per-gateway: swarm.advisory.suicide.<gw_id>
}
