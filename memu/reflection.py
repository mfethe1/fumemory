"""Reflection pipeline: scheduler cadence, proposal generator, and Telegram notifier.

Provides pure, DB-free business logic for:
  - Scheduler cadence decisions (when to run task-close vs idle/dream reflection)
  - Proposal generation from evidence records
  - Compact Telegram digest formatting
  - A TelegramNotifier adapter whose delivery failures degrade gracefully

Database operations (INSERT/UPDATE) live in the API endpoints in api.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REFLECTION_REVIEW_WINDOW_HOURS = 6
TASK_CLOSE_MAX_PROPOSALS = 3
IDLE_DREAM_CADENCE_HOURS = 4


# ---------------------------------------------------------------------------
# Scheduler cadence helpers
# ---------------------------------------------------------------------------

def should_run_task_close_reflection(existing_proposal_count: int) -> bool:
    """Return True if task-close reflection may still emit a proposal.

    Capped at TASK_CLOSE_MAX_PROPOSALS per task to avoid notification spam.
    """
    return existing_proposal_count < TASK_CLOSE_MAX_PROPOSALS


def should_run_idle_dream_reflection(
    last_ran_at: Optional[datetime],
    now: Optional[datetime] = None,
) -> bool:
    """Return True if idle/dream reflection is due to run.

    Idle/dream reflection runs at most once per IDLE_DREAM_CADENCE_HOURS.
    If it has never run (last_ran_at is None) it should run immediately.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if last_ran_at is None:
        return True
    return (now - last_ran_at) >= timedelta(hours=IDLE_DREAM_CADENCE_HOURS)


# ---------------------------------------------------------------------------
# Proposal expiry helpers
# ---------------------------------------------------------------------------

def compute_proposal_expiry(created_at: Optional[datetime] = None) -> datetime:
    """Return the 6-hour review window expiry for a new proposal."""
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    return created_at + timedelta(hours=REFLECTION_REVIEW_WINDOW_HOURS)


def is_proposal_expired(expires_at: datetime, now: Optional[datetime] = None) -> bool:
    """Return True if the review window has closed."""
    if now is None:
        now = datetime.now(timezone.utc)
    return now >= expires_at


# ---------------------------------------------------------------------------
# Task-close reflection worker
# ---------------------------------------------------------------------------

def generate_task_close_proposals(
    task_id: str,
    session_id: Optional[str],
    evidence_records: list[dict[str, Any]],
    existing_proposal_count: int = 0,
) -> list[dict[str, Any]]:
    """Generate at most TASK_CLOSE_MAX_PROPOSALS proposals from task evidence.

    Prioritises failure records (highest learning value) then other evidence.
    Returns plain dicts ready for DB insertion; does not persist anything.
    """
    remaining = TASK_CLOSE_MAX_PROPOSALS - existing_proposal_count
    if remaining <= 0:
        return []

    failures = [r for r in evidence_records if r.get("memory_type") == "failure"]
    others = [r for r in evidence_records if r.get("memory_type") not in ("failure",)]
    ordered = (failures + others)[:remaining]

    proposals: list[dict[str, Any]] = []
    for record in ordered:
        content = record.get("content", "")
        if not content:
            continue
        summary = content[:200] + ("..." if len(content) > 200 else "")
        proposals.append({
            "source": "task_close",
            "summary": summary,
            "content": content,
            "confidence": float(record.get("confidence", 0.7)),
            "risk_flags": _extract_risk_flags(record),
            "source_task_id": task_id,
            "source_session_id": session_id,
            "source_evidence_ids": [str(record["id"])] if record.get("id") else [],
            "agent_id": record.get("agent_id", "unknown"),
        })

    return proposals[:remaining]


# ---------------------------------------------------------------------------
# Idle/dream reflection worker
# ---------------------------------------------------------------------------

def generate_idle_dream_proposals(
    cross_task_evidence: list[dict[str, Any]],
    stale_learning_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Generate cross-task proposals from idle/dream reflection.

    Detects repeated failures across tasks and flags stale learning candidates.
    Returns plain dicts ready for DB insertion; does not persist anything.
    """
    proposals: list[dict[str, Any]] = []

    # Group failures by normalised content prefix to detect repetition
    failure_pattern_tasks: dict[str, list[str]] = {}
    for record in cross_task_evidence:
        if record.get("memory_type") != "failure":
            continue
        content = record.get("content", "")
        if not content:
            continue
        meta = record.get("metadata") or {}
        task_id = meta.get("task_id") or record.get("source_task_id") or "unknown"
        key = content[:100].lower().strip()
        failure_pattern_tasks.setdefault(key, []).append(task_id)

    for pattern, task_ids in failure_pattern_tasks.items():
        if len(task_ids) < 2:
            continue
        unique_tasks = list(dict.fromkeys(task_ids))
        proposals.append({
            "source": "idle_dream",
            "summary": (
                f"Repeated failure pattern across {len(unique_tasks)} task(s): {pattern[:120]}"
            ),
            "content": (
                f"Cross-task failure pattern (tasks: {', '.join(unique_tasks[:5])}): {pattern}"
            ),
            "confidence": min(0.5 + 0.1 * len(unique_tasks), 0.95),
            "risk_flags": ["repeated_failure"],
            "source_task_id": None,
            "source_session_id": None,
            "source_evidence_ids": [],
            "agent_id": "reflection-worker",
        })

    # Flag stale learning candidates (cap at 2 per idle/dream run)
    for candidate in stale_learning_candidates[:2]:
        content = candidate.get("content", "")
        if not content:
            continue
        proposals.append({
            "source": "idle_dream",
            "summary": f"Stale learning candidate: {content[:150]}",
            "content": f"Review learning validity: {content}",
            "confidence": 0.6,
            "risk_flags": ["stale_learning"],
            "source_task_id": None,
            "source_session_id": None,
            "source_evidence_ids": [str(candidate["id"])] if candidate.get("id") else [],
            "agent_id": "reflection-worker",
        })

    return proposals


def _extract_risk_flags(record: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if record.get("memory_type") == "failure":
        flags.append("failure_evidence")
    meta = record.get("metadata") or {}
    risk_score = meta.get("risk_score", 0)
    try:
        if int(risk_score) >= 70:
            flags.append("high_risk")
    except (TypeError, ValueError):
        pass
    return flags


# ---------------------------------------------------------------------------
# Telegram compact notice formatter
# ---------------------------------------------------------------------------

def format_telegram_compact_notice(
    proposal_id: str | UUID,
    summary: str,
    confidence: float,
    risk_flags: list[str],
    source_task_id: Optional[str],
    expires_at: datetime,
    source: str,
) -> str:
    """Return a compact Telegram message for a reflection proposal.

    Contains summary, confidence, risk, expiry, and action commands.
    Raw evidence content is never included.
    """
    pid = str(proposal_id)
    risk_str = ", ".join(risk_flags) if risk_flags else "none"
    source_str = f"task {source_task_id}" if source_task_id else f"[{source}]"
    expiry_str = expires_at.strftime("%Y-%m-%d %H:%M UTC")

    return "\n".join([
        "[Reflection Proposal]",
        f"Source: {source_str}",
        f"Summary: {summary[:200]}",
        f"Confidence: {confidence:.0%}  Risk: {risk_str}",
        f"Expires: {expiry_str}",
        f"Actions: /approve_{pid} | /deny_{pid} | /edit_{pid} | /inspect_{pid}",
    ])


# ---------------------------------------------------------------------------
# Telegram notifier adapter
# ---------------------------------------------------------------------------

SendFn = Callable[[str], Coroutine[Any, Any, Optional[str]]]


class TelegramNotifier:
    """Compact Telegram notification adapter for reflection proposals.

    Wraps an async send function so delivery failures degrade gracefully —
    the proposal lifecycle is never blocked by a Telegram outage.
    """

    def __init__(self, send_fn: Optional[SendFn] = None) -> None:
        self._send = send_fn

    async def send_compact_notice(
        self,
        proposal_id: str | UUID,
        summary: str,
        confidence: float,
        risk_flags: list[str],
        source_task_id: Optional[str],
        expires_at: datetime,
        source: str,
    ) -> Optional[str]:
        """Send a compact Telegram notice.

        Returns the Telegram message ID on success, None on failure.
        Exceptions from the underlying send function are caught and logged.
        """
        message = format_telegram_compact_notice(
            proposal_id=proposal_id,
            summary=summary,
            confidence=confidence,
            risk_flags=risk_flags,
            source_task_id=source_task_id,
            expires_at=expires_at,
            source=source,
        )
        if self._send is None:
            logger.info(
                "TelegramNotifier: no send_fn configured, skipping delivery "
                "for proposal %s",
                proposal_id,
            )
            return None
        try:
            result = await self._send(message)
            return result
        except Exception as exc:
            logger.warning(
                "TelegramNotifier: delivery failed for proposal %s: %s",
                proposal_id,
                exc,
            )
            return None
