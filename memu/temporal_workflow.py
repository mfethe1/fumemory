from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Union


HealthCheckResult = Union[bool, Dict[str, Any]]
HealthCheckCallable = Callable[[dict[str, Any]], Awaitable[HealthCheckResult]]
RepairCallable = Callable[[dict[str, Any]], Awaitable[HealthCheckResult]]
EventCallback = Callable[[dict[str, Any]], Optional[Awaitable[None]]]


@dataclass
class WorkflowState:
    run_id: str
    status: str = "pending"
    step: str = "init"
    attempt: int = 0
    repaired: bool = False
    last_error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class MemUHealthAndRepairWorkflow:
    def __init__(
        self,
        *,
        run_id: str,
        health_check: HealthCheckCallable,
        repair: RepairCallable,
        state_dir: str,
        max_attempts: int = 3,
        retry_delays_seconds: list[int] | None = None,
        on_event: EventCallback | None = None,
    ):
        self.run_id = run_id
        self.health_check = health_check
        self.repair = repair
        self.max_attempts = max_attempts
        self.retry_delays_seconds = retry_delays_seconds or [5, 30, 120]
        self.on_event = on_event
        self.state_path = Path(state_dir).expanduser().resolve() / f"{run_id}.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()

    def _load_state(self) -> WorkflowState:
        if not self.state_path.exists():
            return WorkflowState(run_id=self.run_id)
        payload = json.loads(self.state_path.read_text())
        return WorkflowState(**payload)

    def _save_state(self) -> None:
        self.state.updated_at = datetime.now(timezone.utc).isoformat()
        self.state_path.write_text(json.dumps(asdict(self.state), indent=2, sort_keys=True))

    async def _emit(self, event: str, **payload: Any) -> None:
        envelope = {
            "workflow": "memu_health_and_repair",
            "run_id": self.run_id,
            "event": event,
            "status": self.state.status,
            "step": self.state.step,
            "attempt": self.state.attempt,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        self.state.events.append(envelope)
        self._save_state()
        if self.on_event:
            emitted = self.on_event(envelope)
            if asyncio.iscoroutine(emitted):
                await emitted

    async def _run_health_check(self) -> tuple[bool, dict[str, Any]]:
        self.state.step = "health_check"
        self._save_state()
        await self._emit("health_check_started")
        result = await self.health_check(
            {"attempt": self.state.attempt, "state": asdict(self.state)}
        )
        if isinstance(result, bool):
            return result, {"ok": result}
        return bool(result.get("ok")), result

    async def _run_repair(self) -> tuple[bool, dict[str, Any]]:
        self.state.step = "repair"
        self._save_state()
        await self._emit("repair_started")
        result = await self.repair({"attempt": self.state.attempt, "state": asdict(self.state)})
        if isinstance(result, bool):
            return result, {"repaired": result}
        return bool(result.get("repaired", result.get("ok"))), result

    async def run(self) -> WorkflowState:
        self.state.status = "running"
        self._save_state()
        await self._emit("workflow_started")

        while self.state.attempt < self.max_attempts:
            self.state.attempt += 1
            self._save_state()

            ok, health_payload = await self._run_health_check()
            await self._emit("health_check_finished", ok=ok, payload=health_payload)
            if ok:
                self.state.status = "healthy"
                self.state.step = "complete"
                self._save_state()
                await self._emit("workflow_completed", outcome="healthy")
                return self.state

            repaired, repair_payload = await self._run_repair()
            self.state.repaired = self.state.repaired or repaired
            self._save_state()
            await self._emit("repair_finished", repaired=repaired, payload=repair_payload)

            if repaired:
                verified, verify_payload = await self._run_health_check()
                await self._emit("verification_finished", ok=verified, payload=verify_payload)
                if verified:
                    self.state.status = "recovered"
                    self.state.step = "complete"
                    self._save_state()
                    await self._emit("workflow_completed", outcome="recovered")
                    return self.state

            if self.state.attempt >= self.max_attempts:
                break

            self.state.step = "backoff"
            self._save_state()
            delay_idx = min(self.state.attempt - 1, len(self.retry_delays_seconds) - 1)
            delay = self.retry_delays_seconds[delay_idx]
            await self._emit("retry_scheduled", delay_seconds=delay)
            await asyncio.sleep(delay)

        self.state.status = "escalated"
        self.state.step = "complete"
        self.state.last_error = "max_attempts_exceeded"
        self._save_state()
        await self._emit("workflow_escalated", reason=self.state.last_error)
        return self.state
