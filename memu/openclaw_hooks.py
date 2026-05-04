# memu/openclaw_hooks.py
import sys
import os
import logging
import asyncio
from enum import Enum

import httpx

logger = logging.getLogger("memu.hooks")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
if not logger.handlers:
    logger.addHandler(handler)

MEMU_API_URL = os.environ.get("MEMU_API_URL") or os.environ.get("MEMU_BASE_URL") or "http://127.0.0.1:8000"
MEMU_API_URL = MEMU_API_URL.rstrip("/")
MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "")

# Canonical synchronous write endpoint — immediately searchable evidence.
CANONICAL_WRITE_ENDPOINT = "/api/v1/memu/add"

# Maximum retry attempts for TELEMETRY writes before degrading.
TELEMETRY_MAX_RETRIES = 3


class WriteCriticality(str, Enum):
    """Evidence Criticality Policy: routes writes to blocking or degrading paths.

    COMPLETION_PROOF — required to prove task completion or gateway readiness.
      Failures raise CompletionProofWriteError; the caller must not proceed
      with task completion until the write succeeds or an operator waiver is
      recorded via write_waiver().

    TELEMETRY — non-blocking activity context. Failures retry up to
      TELEMETRY_MAX_RETRIES times, then log degraded without raising.
      Must not be confused with completion proof.
    """
    COMPLETION_PROOF = "completion_proof"
    TELEMETRY = "telemetry"


class CompletionProofWriteError(RuntimeError):
    """Raised when a COMPLETION_PROOF evidence write fails.

    Task completion or gateway readiness must be blocked until the write
    succeeds or an operator waiver is stored via write_waiver().
    """
    def __init__(self, action: str, cause: Exception):
        super().__init__(
            f"Completion-proof write failed for action '{action}': {cause}"
        )
        self.action = action
        self.cause = cause


def _headers() -> dict:
    return {"X-MemU-Key": MEMU_API_KEY}


async def _post_canonical(payload: dict, timeout: float = 10.0) -> dict:
    """POST payload to the canonical synchronous write endpoint.

    Returns parsed JSON response or raises on HTTP/network error.
    /memories/async is NOT used here — canonical writes must be synchronous
    so evidence is immediately searchable.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{MEMU_API_URL}{CANONICAL_WRITE_ENDPOINT}",
            json=payload,
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def _telemetry_write(payload: dict, label: str) -> dict | None:
    """Write with TELEMETRY criticality: retry up to TELEMETRY_MAX_RETRIES, then degrade."""
    last_exc: Exception | None = None
    for attempt in range(1, TELEMETRY_MAX_RETRIES + 1):
        try:
            result = await _post_canonical(payload)
            if attempt > 1:
                logger.info("Telemetry write succeeded on attempt %d: %s", attempt, label)
            return result
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "Telemetry write attempt %d/%d failed for %s: %s",
                attempt, TELEMETRY_MAX_RETRIES, label, exc,
            )
    logger.error(
        "Telemetry write degraded after %d attempts for %s: %s. "
        "Task execution continues (telemetry, not completion proof).",
        TELEMETRY_MAX_RETRIES, label, last_exc,
    )
    return None


async def log_action(
    agent_id: str,
    action: str,
    details: dict,
    criticality: WriteCriticality = WriteCriticality.COMPLETION_PROOF,
    task_id: str | None = None,
    session_id: str | None = None,
    idempotency_key: str | None = None,
):
    """Write an OpenClaw agent action as canonical Evidence Memory.

    Uses /api/v1/memu/add (canonical synchronous write path) so evidence is
    immediately searchable. /memories/async is not used and must not be
    treated as canonical until schema parity is proven.

    Routing follows the Evidence Criticality Policy:
    - COMPLETION_PROOF (default): raises CompletionProofWriteError on failure.
    - TELEMETRY: retries up to TELEMETRY_MAX_RETRIES times, then degrades.
    """
    metadata: dict = {
        "action_type": action,
        "details": details,
        "source": "openclaw_hook",
        "criticality": criticality.value,
    }
    if task_id is not None:
        metadata["task_id"] = task_id
    if session_id is not None:
        metadata["session_id"] = session_id

    payload: dict = {
        "content": f"Action: {action}",
        "agent_id": agent_id,
        "memory_type": "user_action",
        "memory_kind": "evidence",
        "metadata": metadata,
    }
    if idempotency_key is not None:
        payload["idempotency_key"] = idempotency_key

    if criticality == WriteCriticality.COMPLETION_PROOF:
        try:
            result = await _post_canonical(payload)
            logger.info(
                "Completion-proof action write OK: action=%s agent=%s id=%s",
                action, agent_id, result.get("id"),
            )
            return result
        except Exception as exc:
            raise CompletionProofWriteError(action=action, cause=exc) from exc
    else:
        return await _telemetry_write(payload, label=f"action:{action}")


async def log_search(
    agent_id: str,
    query: str,
    results_summary: str,
    task_id: str | None = None,
    session_id: str | None = None,
):
    """Write a web search event as canonical Evidence Memory (TELEMETRY criticality).

    Uses /api/v1/memu/add (canonical synchronous write path).
    Failures retry up to TELEMETRY_MAX_RETRIES times, then degrade without
    blocking task execution. Must not be confused with completion proof.
    """
    metadata: dict = {
        "query": query,
        "type": "web_search",
        "source": "openclaw_hook",
        "criticality": WriteCriticality.TELEMETRY.value,
    }
    if task_id is not None:
        metadata["task_id"] = task_id
    if session_id is not None:
        metadata["session_id"] = session_id

    payload = {
        "content": f"Search: {query}\nResult Summary: {results_summary[:500]}",
        "agent_id": agent_id,
        "memory_type": "external",
        "memory_kind": "evidence",
        "metadata": metadata,
    }
    return await _telemetry_write(payload, label=f"search:{query[:40]}")


async def write_waiver(
    agent_id: str,
    action: str,
    reason: str,
    operator_id: str,
    task_id: str | None = None,
):
    """Record an operator waiver for a failed completion-proof write.

    A waiver evidence record allows task completion to proceed when a
    completion-proof write cannot succeed and a human/operator has explicitly
    accepted the risk. This write is itself COMPLETION_PROOF so that waiver
    evidence is durable.
    """
    metadata: dict = {
        "waiver": True,
        "waiver_for_action": action,
        "waiver_reason": reason,
        "operator_id": operator_id,
        "source": "openclaw_hook",
        "criticality": WriteCriticality.COMPLETION_PROOF.value,
    }
    if task_id is not None:
        metadata["task_id"] = task_id

    payload = {
        "content": (
            f"Operator waiver granted for action '{action}' by {operator_id}. "
            f"Reason: {reason}"
        ),
        "agent_id": agent_id,
        "memory_type": "user_action",
        "memory_kind": "evidence",
        "metadata": metadata,
    }
    try:
        result = await _post_canonical(payload)
        logger.info(
            "Waiver evidence written: action=%s operator=%s id=%s",
            action, operator_id, result.get("id"),
        )
        return result
    except Exception as exc:
        raise CompletionProofWriteError(action=f"waiver:{action}", cause=exc) from exc


async def recall(query: str, agent_id: str):
    """Check if we already know this."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{MEMU_API_URL}/search",
                json={"query": query, "agent_id": agent_id, "limit": 3},
                headers=_headers(),
            )
            if resp.status_code == 200:
                results = resp.json()
                if results and results[0]['final_score'] > 0.8:
                    return results[0]['memory']['content']
    except Exception:
        pass
    return None


if __name__ == "__main__":
    if len(sys.argv) > 2:
        cmd = sys.argv[1]
        if cmd == "log-action":
            asyncio.run(log_action("cli", "test_action", {"test": True}))
        elif cmd == "recall":
            res = asyncio.run(recall(sys.argv[2], "cli"))
            print(res if res else "No recall match.")
