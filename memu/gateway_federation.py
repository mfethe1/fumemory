"""Cross-gateway NATS federation helpers and smoke verifier.

This module centralizes the shared transport contract for Railway-backed
NATS federation across OpenClaw gateways.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import nats

CANONICAL_SUBJECTS: tuple[str, ...] = (
    "swarm.discovery",
    "swarm.warden.heartbeat",
    "swarm.events.heartbeat",
    "swarm.events",
    "AGENT_EVENTS",
    "swarm.rpc.request",
    "swarm.warden.respawn",
    "swarm.warden.status",
    "swarm.blackboard",
    "swarm.blackboard.sync",
    "swarm.blackboard.validate",
)

GATEWAY_ID_PATTERN = re.compile(r"^[a-z0-9-]+$")


@dataclass(slots=True)
class FederationConfig:
    gateway_id: str
    nats_railway_url: str
    nats_auth_token: str | None = None
    memu_base_url: str | None = None
    memu_key: str | None = None
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> "FederationConfig":
        gateway_id = (
            os.environ.get("GATEWAY_ID")
            or os.environ.get("HOSTNAME")
            or os.environ.get("COMPUTERNAME")
            or ""
        ).strip().lower()
        nats_railway_url = os.environ.get("NATS_RAILWAY_URL", "").strip()
        memu_base_url = (
            os.environ.get("MEMU_BASE_URL")
            or os.environ.get("MEMU_API_URL")
            or os.environ.get("MEMU_ENDPOINT")
            or ""
        ).strip() or None
        memu_key = (
            os.environ.get("X_MEMU_KEY")
            or os.environ.get("MEMU_API_KEY")
            or os.environ.get("MEMU_KEY")
            or ""
        ).strip() or None
        timeout_seconds = float(os.environ.get("FEDERATION_SMOKE_TIMEOUT_SECONDS", "10"))
        cfg = cls(
            gateway_id=gateway_id,
            nats_railway_url=nats_railway_url,
            nats_auth_token=os.environ.get("NATS_AUTH_TOKEN") or None,
            memu_base_url=memu_base_url,
            memu_key=memu_key,
            timeout_seconds=timeout_seconds,
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        validate_gateway_id(self.gateway_id)
        if not self.nats_railway_url:
            raise ValueError("NATS_RAILWAY_URL is required")


@dataclass(slots=True)
class SmokeResult:
    name: str
    ok: bool
    detail: str
    data: dict[str, Any] | None = None


class FederationSmokeError(RuntimeError):
    pass


def validate_gateway_id(gateway_id: str) -> None:
    if not gateway_id:
        raise ValueError("GATEWAY_ID is required")
    if not GATEWAY_ID_PATTERN.fullmatch(gateway_id):
        raise ValueError(
            "GATEWAY_ID must match [a-z0-9-]+ and be stable across restarts"
        )


def durable_consumer_name(gateway_id: str, stream: str = "AGENT_EVENTS") -> str:
    validate_gateway_id(gateway_id)
    normalized_stream = re.sub(r"[^A-Z0-9]+", "_", stream.upper()).strip("_")
    normalized_gateway = re.sub(r"[^A-Z0-9]+", "_", gateway_id.upper()).strip("_")
    return f"MEMU_{normalized_gateway}_{normalized_stream}"


def response_subject(gateway_id: str) -> str:
    validate_gateway_id(gateway_id)
    return f"swarm.rpc.response.{gateway_id}"


def allowed_subject(subject: str, gateway_id: str) -> bool:
    validate_gateway_id(gateway_id)
    if subject in CANONICAL_SUBJECTS:
        return True
    if subject == response_subject(gateway_id):
        return True
    if subject == f"swarm.advisory.suicide.{gateway_id}":
        return True
    if subject.startswith("swarm.checkpoint."):
        task_id = subject.removeprefix("swarm.checkpoint.")
        return bool(task_id)
    return False


def build_gateway_announce(
    gateway_id: str,
    *,
    role: str = "general",
    capabilities: list[str] | None = None,
    status: str = "ready",
) -> dict[str, Any]:
    validate_gateway_id(gateway_id)
    return {
        "gateway_id": gateway_id,
        "ts": utc_now_iso(),
        "role": role,
        "capabilities": capabilities or ["memu", "swarm"],
        "status": status,
        "version": "1",
        "transport": {
            "railway_nats": True,
            "agent_events": True,
        },
    }


def build_dispatch_envelope(
    *,
    source_gateway_id: str,
    title: str,
    prompt: str,
    target_gateway_id: str | None = None,
    capabilities: list[str] | None = None,
    ttl_seconds: int = 300,
    max_agents: int = 3,
    max_runtime_seconds: int = 600,
) -> dict[str, Any]:
    validate_gateway_id(source_gateway_id)
    if target_gateway_id:
        validate_gateway_id(target_gateway_id)
    root_task_id = f"task_{uuid4().hex[:12]}"
    step_name = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "dispatch"
    dispatch_id = str(uuid4())
    return {
        "version": 1,
        "dispatch_id": dispatch_id,
        "root_task_id": root_task_id,
        "source_gateway_id": source_gateway_id,
        "target_gateway_id": target_gateway_id,
        "kind": "task.dispatch",
        "ts": utc_now_iso(),
        "ttl_seconds": ttl_seconds,
        "idempotency_key": ":".join(
            part
            for part in [root_task_id, target_gateway_id or "broadcast", step_name]
            if part
        ),
        "capabilities": capabilities or ["research"],
        "payload": {
            "title": title,
            "prompt": prompt,
            "priority": "normal",
            "budget": {
                "max_agents": max_agents,
                "max_runtime_seconds": max_runtime_seconds,
            },
        },
    }


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


async def run_smoke_checks(
    cfg: FederationConfig,
    *,
    run_memu_check: bool = True,
) -> list[SmokeResult]:
    cfg.validate()
    results: list[SmokeResult] = []

    results.append(
        SmokeResult(
            name="config",
            ok=True,
            detail="required federation config present",
            data={
                "gateway_id": cfg.gateway_id,
                "nats_railway_url": cfg.nats_railway_url,
                "durable_consumer": durable_consumer_name(cfg.gateway_id),
            },
        )
    )

    connect_kwargs: dict[str, Any] = {
        "servers": [cfg.nats_railway_url],
        "connect_timeout": cfg.timeout_seconds,
        "allow_reconnect": False,
    }
    if cfg.nats_auth_token:
        connect_kwargs["token"] = cfg.nats_auth_token

    nc = await nats.connect(**connect_kwargs)
    try:
        js = nc.jetstream()
        account = await js.account_info()
        results.append(
            SmokeResult(
                name="connect",
                ok=True,
                detail="connected to Railway NATS and JetStream",
                data={
                    "memory": getattr(account, "memory", None),
                    "storage": getattr(account, "storage", None),
                },
            )
        )

        marker = f"federation-smoke-{cfg.gateway_id}-{uuid4().hex[:10]}"

        async def _expect_message(subject: str, payload: dict[str, Any]) -> SmokeResult:
            future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

            async def _cb(msg):
                if not future.done():
                    future.set_result(msg.data)

            sub = await nc.subscribe(subject, cb=_cb)
            try:
                await nc.flush()
                await nc.publish(subject, json.dumps(payload).encode())
                await nc.flush()
                raw = await asyncio.wait_for(future, timeout=cfg.timeout_seconds)
                decoded = json.loads(raw.decode())
                return SmokeResult(
                    name=f"subject:{subject}",
                    ok=True,
                    detail="publish/consume succeeded",
                    data=decoded,
                )
            finally:
                await sub.unsubscribe()

        announce = build_gateway_announce(cfg.gateway_id, capabilities=["memu", "swarm", "qa"])
        announce["marker"] = marker
        results.append(await _expect_message("swarm.discovery", announce))

        dispatch = build_dispatch_envelope(
            source_gateway_id=cfg.gateway_id,
            title="Federation smoke dispatch",
            prompt=f"Synthetic federation dispatch {marker}",
            target_gateway_id=cfg.gateway_id,
            capabilities=["qa"],
        )
        dispatch["marker"] = marker
        results.append(await _expect_message("swarm.events", dispatch))

        directed = {
            "version": 1,
            "kind": "rpc.response",
            "gateway_id": cfg.gateway_id,
            "marker": marker,
            "ts": utc_now_iso(),
        }
        results.append(await _expect_message(response_subject(cfg.gateway_id), directed))

        if run_memu_check and cfg.memu_base_url and cfg.memu_key:
            results.extend(await run_memu_smoke(cfg, marker=marker))
        elif run_memu_check:
            results.append(
                SmokeResult(
                    name="memu",
                    ok=False,
                    detail="memU smoke skipped because MEMU base URL or key missing",
                )
            )
    finally:
        await nc.close()

    return results


async def run_memu_smoke(cfg: FederationConfig, *, marker: str) -> list[SmokeResult]:
    headers = {
        "Content-Type": "application/json",
        "X-MemU-Key": cfg.memu_key or "",
        "X-API-Key": cfg.memu_key or "",
    }
    base = (cfg.memu_base_url or "").rstrip("/")
    if not base:
        return [SmokeResult(name="memu", ok=False, detail="MEMU base URL missing")]

    async with httpx.AsyncClient(timeout=cfg.timeout_seconds) as client:
        results: list[SmokeResult] = []

        health = await client.get(f"{base}/api/v1/memu/health", headers=headers)
        results.append(
            SmokeResult(
                name="memu-health",
                ok=health.status_code == 200,
                detail=f"health status={health.status_code}",
            )
        )

        write_payload = {
            "content": f"{marker} gateway federation smoke write",
            "agent_id": cfg.gateway_id,
            "memory_type": "observation",
            "metadata": {"marker": marker, "source": "gateway-federation-smoke"},
        }
        write = await client.post(f"{base}/api/v1/memu/add", headers=headers, json=write_payload)
        results.append(
            SmokeResult(
                name="memu-write",
                ok=write.status_code == 200,
                detail=f"write status={write.status_code}",
                data=safe_json(write),
            )
        )

        search = await client.post(
            f"{base}/api/v1/memu/search-text",
            headers=headers,
            json={"query": marker, "k": 5},
        )
        results.append(
            SmokeResult(
                name="memu-search-text",
                ok=search.status_code == 200,
                detail=f"search-text status={search.status_code}",
                data=safe_json(search),
            )
        )

        return results


def safe_json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    except Exception:
        return None


def results_to_json(results: list[SmokeResult]) -> dict[str, Any]:
    return {
        "ok": all(item.ok for item in results),
        "results": [asdict(item) for item in results],
    }
