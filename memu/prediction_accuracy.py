"""Prediction Accuracy Tracker + Graduated Circuit Breaker.

Consumes prediction outcome events from NATS JetStream and maintains
rolling accuracy metrics in NATS KV. Agent-scoped keys prevent write conflicts.

KV Bucket: PREDICTION_ACCURACY
Key format: <agent_id>.<intent>.hits / .misses / .dismissals / .accuracy_pct / .last_updated
Special key: <agent_id>.circuit_breaker (state machine)

Deliberation consensus: predict-intent-20260306 + predict-impl-20260306
"""

from __future__ import annotations

import enum
import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class BreakerState(str, enum.Enum):
    LEARNING = "learning"       # Days 1-30: 50% miss tolerance
    MATURING = "maturing"       # Days 31-60: 30% miss tolerance
    STEADY = "steady"           # Day 61+: 3 consecutive misses = trip
    TRIPPED = "tripped"         # Paused — hints only


@dataclass
class CircuitBreakerStatus:
    state: BreakerState
    consecutive_misses: int
    consecutive_hits: int
    total_predictions: int
    total_hits: int
    total_misses: int
    total_dismissals: int
    start_timestamp: str  # when the system started (for state transitions)
    last_updated: str

    @property
    def accuracy(self) -> float:
        total = self.total_hits + self.total_misses
        if total == 0:
            return 0.0
        return self.total_hits / total

    @property
    def miss_rate(self) -> float:
        return 1.0 - self.accuracy

    def should_trip(self, days_active: int) -> bool:
        """Check if breaker should trip based on current state."""
        if self.state == BreakerState.TRIPPED:
            return False  # Already tripped

        if self.state == BreakerState.LEARNING:
            # Trip at 50% miss rate over 20+ predictions
            return self.total_predictions >= 20 and self.miss_rate > 0.50

        if self.state == BreakerState.MATURING:
            # Trip at 30% miss rate
            return self.total_predictions >= 20 and self.miss_rate > 0.30

        if self.state == BreakerState.STEADY:
            # Trip at 3 consecutive misses
            return self.consecutive_misses >= 3

        return False

    def should_recover(self) -> bool:
        """Check if tripped breaker should recover (5 consecutive hits)."""
        return self.state == BreakerState.TRIPPED and self.consecutive_hits >= 5

    def advance_state(self, days_active: int) -> BreakerState:
        """Determine correct state based on time since start."""
        if self.state == BreakerState.TRIPPED:
            return BreakerState.TRIPPED
        if days_active >= 61:
            return BreakerState.STEADY
        if days_active >= 31:
            return BreakerState.MATURING
        return BreakerState.LEARNING


class PredictionAccuracyTracker:
    """Tracks prediction accuracy via NATS KV.

    Usage:
        tracker = PredictionAccuracyTracker(js)
        await tracker.record_hit("lenny", "status_check", 0.85)
        await tracker.record_miss("lenny", "status_check", "remediation")
        status = await tracker.get_breaker_status("lenny")
    """

    def __init__(self, js):
        self.js = js
        self._kv = None

    async def _get_kv(self):
        if self._kv is None:
            self._kv = await self.js.key_value("PREDICTION_ACCURACY")
        return self._kv

    async def _get_val(self, key: str, default: Any = None) -> Any:
        kv = await self._get_kv()
        try:
            entry = await kv.get(key)
            return json.loads(entry.value.decode()) if entry.value else default
        except Exception:
            return default

    async def _put_val(self, key: str, value: Any) -> None:
        kv = await self._get_kv()
        await kv.put(key, json.dumps(value).encode())

    async def _increment(self, key: str, amount: int = 1) -> int:
        """Increment a counter key. Returns new value."""
        current = await self._get_val(key, 0)
        new_val = current + amount
        await self._put_val(key, new_val)
        return new_val

    async def record_hit(
        self,
        agent_id: str,
        predicted_intent: str,
        confidence: float,
    ) -> CircuitBreakerStatus:
        """Record a prediction hit (user did what we predicted)."""
        await self._increment(f"{agent_id}.{predicted_intent}.hits")
        await self._put_val(f"{agent_id}.{predicted_intent}.last_updated", datetime.now(timezone.utc).isoformat())

        # Update accuracy percentage
        hits = await self._get_val(f"{agent_id}.{predicted_intent}.hits", 0)
        misses = await self._get_val(f"{agent_id}.{predicted_intent}.misses", 0)
        total = hits + misses
        accuracy = hits / total if total > 0 else 0.0
        await self._put_val(f"{agent_id}.{predicted_intent}.accuracy_pct", round(accuracy * 100, 1))

        # Update circuit breaker
        return await self._update_breaker(agent_id, "hit")

    async def record_miss(
        self,
        agent_id: str,
        predicted_intent: str,
        actual_intent: str,
    ) -> CircuitBreakerStatus:
        """Record a prediction miss (user did something different)."""
        await self._increment(f"{agent_id}.{predicted_intent}.misses")
        await self._put_val(f"{agent_id}.{predicted_intent}.last_updated", datetime.now(timezone.utc).isoformat())

        hits = await self._get_val(f"{agent_id}.{predicted_intent}.hits", 0)
        misses = await self._get_val(f"{agent_id}.{predicted_intent}.misses", 0)
        total = hits + misses
        accuracy = hits / total if total > 0 else 0.0
        await self._put_val(f"{agent_id}.{predicted_intent}.accuracy_pct", round(accuracy * 100, 1))

        return await self._update_breaker(agent_id, "miss")

    async def record_dismissal(
        self,
        agent_id: str,
        predicted_intent: str,
        dismiss_type: str,  # "explicit" or "timeout"
    ) -> None:
        """Record a prediction dismissal (user ignored or dismissed)."""
        await self._increment(f"{agent_id}.{predicted_intent}.dismissals")
        await self._put_val(f"{agent_id}.{predicted_intent}.last_updated", datetime.now(timezone.utc).isoformat())

    async def get_accuracy(self, agent_id: str, intent: str) -> dict:
        """Get accuracy stats for a specific agent+intent pair."""
        hits = await self._get_val(f"{agent_id}.{intent}.hits", 0)
        misses = await self._get_val(f"{agent_id}.{intent}.misses", 0)
        dismissals = await self._get_val(f"{agent_id}.{intent}.dismissals", 0)
        accuracy = await self._get_val(f"{agent_id}.{intent}.accuracy_pct", 0.0)
        return {
            "agent_id": agent_id,
            "intent": intent,
            "hits": hits,
            "misses": misses,
            "dismissals": dismissals,
            "accuracy_pct": accuracy,
            "total_predictions": hits + misses,
        }

    async def _get_breaker(self, agent_id: str) -> CircuitBreakerStatus:
        """Get or create circuit breaker status for an agent."""
        data = await self._get_val(f"{agent_id}.circuit_breaker")
        if data:
            data["state"] = BreakerState(data["state"])
            return CircuitBreakerStatus(**data)

        # Initialize
        now = datetime.now(timezone.utc).isoformat()
        status = CircuitBreakerStatus(
            state=BreakerState.LEARNING,
            consecutive_misses=0,
            consecutive_hits=0,
            total_predictions=0,
            total_hits=0,
            total_misses=0,
            total_dismissals=0,
            start_timestamp=now,
            last_updated=now,
        )
        await self._put_val(f"{agent_id}.circuit_breaker", asdict(status))
        return status

    async def _update_breaker(self, agent_id: str, outcome: str) -> CircuitBreakerStatus:
        """Update circuit breaker state after a prediction outcome."""
        status = await self._get_breaker(agent_id)
        status.total_predictions += 1
        status.last_updated = datetime.now(timezone.utc).isoformat()

        if outcome == "hit":
            status.total_hits += 1
            status.consecutive_hits += 1
            status.consecutive_misses = 0

            # Check recovery
            if status.should_recover():
                status.state = BreakerState.LEARNING  # Reset to learning
                status.consecutive_hits = 0
                logger.info(f"Circuit breaker RECOVERED for {agent_id}")

        elif outcome == "miss":
            status.total_misses += 1
            status.consecutive_misses += 1
            status.consecutive_hits = 0

        # Time-based state advancement
        start = datetime.fromisoformat(status.start_timestamp)
        days_active = (datetime.now(timezone.utc) - start).days
        new_state = status.advance_state(days_active)
        if new_state != status.state and status.state != BreakerState.TRIPPED:
            logger.info(f"Circuit breaker state advanced: {status.state} → {new_state} for {agent_id}")
            status.state = new_state

        # Check trip
        if status.should_trip(days_active):
            status.state = BreakerState.TRIPPED
            logger.warning(f"Circuit breaker TRIPPED for {agent_id} (miss_rate={status.miss_rate:.1%})")

        await self._put_val(f"{agent_id}.circuit_breaker", asdict(status))
        return status

    async def get_breaker_status(self, agent_id: str) -> CircuitBreakerStatus:
        """Public method to check circuit breaker state."""
        return await self._get_breaker(agent_id)

    async def is_predictions_enabled(self, agent_id: str) -> bool:
        """Check if predictions are enabled (breaker not tripped)."""
        status = await self._get_breaker(agent_id)
        return status.state != BreakerState.TRIPPED
