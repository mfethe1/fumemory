"""Session Mode Detector — classifies sessions by purpose.

Combines source-based classification (cron/heartbeat/human) with
content-based keyword matching for human sessions.

Stores current mode in NATS KV SESSION_STATE.

Modes: trading, development, admin, research, social, general

Deliberation consensus: predict-impl-20260306
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class SessionMode:
    TRADING = "trading"
    DEVELOPMENT = "development"
    ADMIN = "admin"
    RESEARCH = "research"
    SOCIAL = "social"
    GENERAL = "general"


# Keyword vocabularies per mode
MODE_VOCABULARIES: dict[str, list[str]] = {
    SessionMode.TRADING: [
        "ticker", "option", "spread", "position", "call", "put", "strike",
        "expir", "premium", "delta", "theta", "gamma", "vega", "iv",
        "market", "trade", "stock", "etf", "spy", "qqq", "price",
        "earnings", "vol", "chart", "signal", "watchlist", "orats",
        "schwab", "portfolio", "pnl", "profit", "loss", "entry", "exit",
    ],
    SessionMode.DEVELOPMENT: [
        "pr", "pull request", "merge", "commit", "push", "branch", "deploy",
        "test", "build", "lint", "ci", "cd", "pipeline", "endpoint",
        "api", "bug", "fix", "feature", "refactor", "typescript", "python",
        "react", "next.js", "node", "npm", "railway", "vercel", "docker",
        "git", "github", "code", "function", "class", "import", "error",
    ],
    SessionMode.ADMIN: [
        "cron", "mount", "status", "health", "restart", "gateway", "config",
        "service", "systemd", "nats", "tailscale", "ssh", "dns", "cert",
        "backup", "disk", "memory", "cpu", "process", "log", "uptime",
        "openclaw", "heartbeat", "pulse", "standup", "check",
    ],
    SessionMode.RESEARCH: [
        "compare", "evaluate", "analyze", "benchmark", "study", "paper",
        "article", "finding", "data", "statistic", "trend", "insight",
        "source", "reference", "investigate", "review", "assessment",
        "competitive", "market research", "report", "synthesis",
    ],
    SessionMode.SOCIAL: [
        "hey", "hello", "hi", "thanks", "good morning", "good night",
        "how are you", "what's up", "lol", "haha", "nice", "cool",
        "agree", "disagree", "funny", "interesting",
    ],
}


class SessionModeDetector:
    """Classifies sessions by purpose using source + content signals.

    Usage:
        detector = SessionModeDetector(js)
        mode = await detector.detect("lenny", source="cron", content="")
        mode = await detector.detect("lenny", source="human", content="check the SPY options chain")
        current = await detector.get_current_mode("session_123")
    """

    CONFIDENCE_THRESHOLD = 0.40  # minimum keyword hit fraction to classify

    def __init__(self, js=None):
        self.js = js
        self._kv = None
        # Pre-compile patterns for efficiency
        self._patterns: dict[str, re.Pattern] = {}
        for mode, words in MODE_VOCABULARIES.items():
            escaped = [re.escape(w) for w in words]
            self._patterns[mode] = re.compile(
                r"\b(" + "|".join(escaped) + r")\b", re.IGNORECASE
            )

    async def _get_kv(self):
        if self._kv is None and self.js:
            self._kv = await self.js.key_value("SESSION_STATE")
        return self._kv

    def classify_source(self, source: str) -> str | None:
        """Classify from session source (per Macklemore's challenge)."""
        source = (source or "").lower().strip()
        source_map = {
            "cron": SessionMode.ADMIN,
            "heartbeat": SessionMode.ADMIN,
            "pulse": SessionMode.ADMIN,
            "scheduled": SessionMode.ADMIN,
            "system": SessionMode.ADMIN,
        }
        return source_map.get(source)

    def classify_content(self, content: str) -> tuple[str, float]:
        """Classify from message content using keyword matching.

        Returns (mode, confidence) where confidence is the fraction
        of total keyword hits belonging to the winning mode.
        """
        if not content or len(content.strip()) < 3:
            return SessionMode.GENERAL, 0.0

        hits: Counter = Counter()
        for mode, pattern in self._patterns.items():
            matches = pattern.findall(content)
            hits[mode] = len(matches)

        total_hits = sum(hits.values())
        if total_hits == 0:
            return SessionMode.GENERAL, 0.0

        top_mode, top_count = hits.most_common(1)[0]
        confidence = top_count / total_hits

        if confidence < self.CONFIDENCE_THRESHOLD:
            return SessionMode.GENERAL, confidence

        return top_mode, confidence

    async def detect(
        self,
        session_id: str,
        source: str = "human",
        content: str = "",
        store: bool = True,
    ) -> dict[str, Any]:
        """Detect session mode from source + content.

        Returns dict with mode, confidence, method, and details.
        """
        # 1. Source-based classification (highest priority for non-human)
        source_mode = self.classify_source(source)
        if source_mode:
            result = {
                "mode": source_mode,
                "confidence": 0.95,
                "method": "source",
                "source": source,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if store:
                await self._store_mode(session_id, result)
            return result

        # 2. Content-based classification
        mode, confidence = self.classify_content(content)
        result = {
            "mode": mode,
            "confidence": round(confidence, 3),
            "method": "content",
            "source": source,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if store:
            await self._store_mode(session_id, result)
        return result

    async def _store_mode(self, session_id: str, result: dict) -> None:
        """Store detected mode in NATS KV."""
        kv = await self._get_kv()
        if kv:
            try:
                await kv.put(session_id, json.dumps(result).encode())
            except Exception as e:
                logger.warning(f"Failed to store session mode: {e}")

    async def get_current_mode(self, session_id: str) -> dict | None:
        """Get the currently detected mode for a session."""
        kv = await self._get_kv()
        if not kv:
            return None
        try:
            entry = await kv.get(session_id)
            return json.loads(entry.value.decode()) if entry.value else None
        except Exception:
            return None

    async def update_with_message(
        self,
        session_id: str,
        message: str,
        source: str = "human",
    ) -> dict:
        """Update mode detection with a new message.

        Can be called on each user message to refine classification.
        Uses cumulative content for better accuracy.
        """
        current = await self.get_current_mode(session_id)
        if current and current.get("method") == "source":
            # Source-classified sessions don't get reclassified by content
            return current

        return await self.detect(session_id, source=source, content=message)
