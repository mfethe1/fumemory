"""Safe Precompute Gate — decides if a predicted action can be pre-executed.

Only allows pre-computation of read-only, side-effect-free actions when
the circuit breaker is not tripped and confidence meets the threshold.

Publishes GATE_DECISION events to NATS for auditability (per Rosie's challenge).

Deliberation consensus: predict-impl-20260306
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from memu.prediction_accuracy import PredictionAccuracyTracker, BreakerState

logger = logging.getLogger(__name__)


@dataclass
class GateDecision:
    action: str
    passed: bool
    reason: str
    confidence: float
    is_read_only: bool
    has_side_effects: bool
    breaker_state: str
    timestamp: str


# Actions known to be safe for pre-computation
SAFE_ACTIONS = frozenset({
    "status_check",
    "health_check",
    "file_read",
    "search_preload",
    "log_fetch",
    "cron_status",
    "git_status",
    "trading_snapshot",
    "memory_search",
    "dependency_check",
})

# Actions that must NEVER be pre-computed
FORBIDDEN_ACTIONS = frozenset({
    "deploy",
    "message_send",
    "file_write",
    "file_delete",
    "config_change",
    "cron_modify",
    "git_push",
    "trade_execute",
    "account_change",
    "secret_rotate",
})


class PrecomputeGate:
    """Gate that decides whether a predicted action can be safely pre-computed.

    Usage:
        gate = PrecomputeGate(accuracy_tracker, nats_js)
        decision = await gate.check("lenny", "status_check", confidence=0.85)
        if decision.passed:
            # safe to pre-compute
    """

    # Confidence thresholds by tier (from Winnie's proposal)
    # Phase 1: only Tier 3 (hints), so gate threshold is for pre-loading data
    TIER3_THRESHOLD = 0.60  # hints
    TIER2_THRESHOLD = 0.75  # suggestions (Phase 3)
    TIER1_THRESHOLD = 0.90  # auto-execute (Phase 4)

    def __init__(self, tracker: PredictionAccuracyTracker, js=None):
        self.tracker = tracker
        self.js = js
        self._kv = None

    async def _publish_decision(self, agent_id: str, decision: GateDecision) -> None:
        """Publish gate decision to NATS for auditability."""
        if not self.js:
            return
        try:
            data = json.dumps({
                "action": decision.action,
                "passed": decision.passed,
                "reason": decision.reason,
                "confidence": decision.confidence,
                "is_read_only": decision.is_read_only,
                "has_side_effects": decision.has_side_effects,
                "breaker_state": decision.breaker_state,
                "timestamp": decision.timestamp,
            }).encode()
            await self.js.publish(f"predict.gate.{agent_id}", data)
        except Exception as e:
            logger.warning(f"Failed to publish gate decision: {e}")

    async def check(
        self,
        agent_id: str,
        action: str,
        confidence: float,
        is_read_only: bool | None = None,
        has_side_effects: bool | None = None,
    ) -> GateDecision:
        """Check if an action is safe to pre-compute.

        Returns a GateDecision with passed=True/False and reason.
        """
        now = datetime.now(timezone.utc).isoformat()

        # Auto-classify if not provided
        if is_read_only is None:
            is_read_only = action in SAFE_ACTIONS
        if has_side_effects is None:
            has_side_effects = action in FORBIDDEN_ACTIONS

        # Get breaker state
        breaker = await self.tracker.get_breaker_status(agent_id)
        breaker_state = breaker.state.value

        # Gate checks (ordered by importance)
        # 1. Forbidden action
        if action in FORBIDDEN_ACTIONS:
            decision = GateDecision(
                action=action, passed=False, reason="Action is in FORBIDDEN list — never pre-compute",
                confidence=confidence, is_read_only=False, has_side_effects=True,
                breaker_state=breaker_state, timestamp=now,
            )
            await self._publish_decision(agent_id, decision)
            return decision

        # 2. Circuit breaker tripped
        if breaker.state == BreakerState.TRIPPED:
            decision = GateDecision(
                action=action, passed=False, reason="Circuit breaker is TRIPPED — hints only mode",
                confidence=confidence, is_read_only=is_read_only, has_side_effects=has_side_effects,
                breaker_state=breaker_state, timestamp=now,
            )
            await self._publish_decision(agent_id, decision)
            return decision

        # 3. Side effects
        if has_side_effects:
            decision = GateDecision(
                action=action, passed=False, reason="Action has side effects — requires user trigger",
                confidence=confidence, is_read_only=is_read_only, has_side_effects=True,
                breaker_state=breaker_state, timestamp=now,
            )
            await self._publish_decision(agent_id, decision)
            return decision

        # 4. Not read-only
        if not is_read_only:
            decision = GateDecision(
                action=action, passed=False, reason="Action is not read-only — requires user trigger",
                confidence=confidence, is_read_only=False, has_side_effects=has_side_effects,
                breaker_state=breaker_state, timestamp=now,
            )
            await self._publish_decision(agent_id, decision)
            return decision

        # 5. Confidence below threshold
        if confidence < self.TIER3_THRESHOLD:
            decision = GateDecision(
                action=action, passed=False,
                reason=f"Confidence {confidence:.2f} below Tier 3 threshold {self.TIER3_THRESHOLD}",
                confidence=confidence, is_read_only=True, has_side_effects=False,
                breaker_state=breaker_state, timestamp=now,
            )
            await self._publish_decision(agent_id, decision)
            return decision

        # All checks passed
        decision = GateDecision(
            action=action, passed=True, reason="All gate checks passed — safe to pre-compute",
            confidence=confidence, is_read_only=True, has_side_effects=False,
            breaker_state=breaker_state, timestamp=now,
        )
        await self._publish_decision(agent_id, decision)
        logger.info(f"Gate PASSED for {agent_id}/{action} (confidence={confidence:.2f})")
        return decision
