"""Negative Feedback Loop + Weight Adjustment for predictions.

Tracks explicit dismissals, implicit ignores (300s timeout per Lenny's challenge),
and wrong-intent misses. Adjusts per-intent prediction weights in NATS KV.

KV Bucket: PREDICTION_WEIGHTS
Key format: <agent_id>.<intent>.weight (default: 1.0)

Downweight rules (consensus):
  - 3x implicit ignore → weight *= 0.5
  - 1x explicit dismiss → weight *= 0.2
  - 1x hit after downweight → weight *= 1.3 (recovery)

Deliberation consensus: predict-impl-20260306
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class FeedbackWeightAdjuster:
    """Adjusts prediction weights based on user feedback signals.

    Usage:
        adjuster = FeedbackWeightAdjuster(js)
        await adjuster.on_explicit_dismiss("lenny", "status_check")
        await adjuster.on_implicit_ignore("lenny", "status_check")
        await adjuster.on_hit_after_downweight("lenny", "status_check")
        weight = await adjuster.get_weight("lenny", "status_check")
    """

    DEFAULT_WEIGHT = 1.0
    EXPLICIT_DISMISS_FACTOR = 0.2
    IMPLICIT_IGNORE_FACTOR = 0.5
    RECOVERY_FACTOR = 1.3
    MIN_WEIGHT = 0.05   # Floor — never fully suppress
    MAX_WEIGHT = 2.0    # Ceiling — prevent runaway recovery
    IGNORE_THRESHOLD = 3  # Number of implicit ignores before downweight
    IMPLICIT_IGNORE_TIMEOUT_SEC = 300  # 5 minutes (per Lenny's challenge)

    def __init__(self, js):
        self.js = js
        self._kv = None
        self._ignore_kv = None

    async def _get_kv(self):
        if self._kv is None:
            self._kv = await self.js.key_value("PREDICTION_WEIGHTS")
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

    async def get_weight(self, agent_id: str, intent: str) -> float:
        """Get current prediction weight for an intent."""
        return await self._get_val(f"{agent_id}.{intent}.weight", self.DEFAULT_WEIGHT)

    async def _set_weight(self, agent_id: str, intent: str, weight: float) -> float:
        """Set weight with floor/ceiling enforcement."""
        clamped = max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, weight))
        await self._put_val(f"{agent_id}.{intent}.weight", round(clamped, 4))
        return clamped

    async def on_explicit_dismiss(self, agent_id: str, intent: str) -> float:
        """User explicitly dismissed a prediction. Heavy downweight."""
        current = await self.get_weight(agent_id, intent)
        new_weight = current * self.EXPLICIT_DISMISS_FACTOR
        result = await self._set_weight(agent_id, intent, new_weight)
        logger.info(
            f"Explicit dismiss: {agent_id}/{intent} weight {current:.3f} → {result:.3f}"
        )
        # Reset ignore counter
        await self._put_val(f"{agent_id}.{intent}.ignore_count", 0)
        return result

    async def on_implicit_ignore(self, agent_id: str, intent: str) -> float:
        """User ignored a prediction (no interaction within 300s).

        Only downweights after IGNORE_THRESHOLD consecutive ignores.
        """
        count_key = f"{agent_id}.{intent}.ignore_count"
        count = await self._get_val(count_key, 0)
        count += 1
        await self._put_val(count_key, count)

        if count >= self.IGNORE_THRESHOLD:
            current = await self.get_weight(agent_id, intent)
            new_weight = current * self.IMPLICIT_IGNORE_FACTOR
            result = await self._set_weight(agent_id, intent, new_weight)
            await self._put_val(count_key, 0)  # Reset counter
            logger.info(
                f"Implicit ignore threshold reached ({count}x): "
                f"{agent_id}/{intent} weight {current:.3f} → {result:.3f}"
            )
            return result

        current = await self.get_weight(agent_id, intent)
        logger.debug(f"Implicit ignore {count}/{self.IGNORE_THRESHOLD}: {agent_id}/{intent}")
        return current

    async def on_hit(self, agent_id: str, intent: str) -> float:
        """User did what was predicted. Recovery if previously downweighted."""
        current = await self.get_weight(agent_id, intent)

        # Reset ignore counter on hit
        await self._put_val(f"{agent_id}.{intent}.ignore_count", 0)

        # Recovery only if below default
        if current < self.DEFAULT_WEIGHT:
            new_weight = current * self.RECOVERY_FACTOR
            result = await self._set_weight(agent_id, intent, new_weight)
            logger.info(
                f"Recovery hit: {agent_id}/{intent} weight {current:.3f} → {result:.3f}"
            )
            return result

        return current

    async def get_all_weights(self, agent_id: str) -> dict[str, float]:
        """Get all weights for an agent."""
        kv = await self._get_kv()
        weights = {}
        try:
            keys = await kv.keys()
            prefix = f"{agent_id}."
            suffix = ".weight"
            for key in keys:
                if key.startswith(prefix) and key.endswith(suffix):
                    intent = key[len(prefix):-len(suffix)]
                    weights[intent] = await self._get_val(key, self.DEFAULT_WEIGHT)
        except Exception:
            pass
        return weights

    async def should_show_prediction(self, agent_id: str, intent: str, confidence: float) -> bool:
        """Check if a prediction should be surfaced based on weight + confidence."""
        weight = await self.get_weight(agent_id, intent)
        effective_confidence = confidence * weight
        # Suppress if effective confidence drops below a usability floor
        return effective_confidence >= 0.30
