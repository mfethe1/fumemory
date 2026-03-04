from __future__ import annotations

from collections import Counter
from typing import Any

import asyncpg


def classify_text_to_intent(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in ["status", "health", "uptime", "down", "error"]):
        return "status_check"
    if any(k in t for k in ["fix", "patch", "repair", "resolve", "remediate"]):
        return "remediation"
    if any(k in t for k in ["deploy", "release", "rollout", "ship"]):
        return "deploy"
    if any(k in t for k in ["plan", "roadmap", "strategy", "implement"]):
        return "planning"
    if any(k in t for k in ["summary", "recap", "update"]):
        return "summary"
    return "general_followup"


async def infer_next_intents(
    conn: asyncpg.Connection,
    user_id: str,
    signal: str,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Infer likely next intents from recent behavior + memory corpus.

    Hybrid scoring (simple/robust):
    - sequence frequency in search_history (same user)
    - lexical intent from recent memory/user_action content
    - recency bias
    """
    # 1) Pull recent query stream for this user/agent.
    rows = await conn.fetch(
        """
        SELECT query, created_at
        FROM search_history
        WHERE agent_id = $1
        ORDER BY created_at DESC
        LIMIT 40
        """,
        user_id,
    )

    intents = [classify_text_to_intent(r["query"]) for r in rows]

    # sequence pairs: current->next in reverse chronological list
    pairs: list[tuple[str, str]] = []
    for i in range(len(intents) - 1):
        current_i = intents[i]
        next_i = intents[i + 1]
        pairs.append((next_i, current_i))

    signal_intent = classify_text_to_intent(signal)

    # 2) Count transitions where previous intent resembles current signal.
    trans_counter = Counter([n for prev, n in pairs if prev == signal_intent])

    # 3) Backfill from recent memories if sparse.
    if not trans_counter:
        mem_rows = await conn.fetch(
            """
            SELECT content
            FROM memories
            WHERE agent_id = $1
              AND memory_type IN ('user_action', 'decision', 'lesson', 'pattern')
            ORDER BY created_at DESC
            LIMIT 25
            """,
            user_id,
        )
        mem_intents = [classify_text_to_intent(m["content"]) for m in mem_rows]
        trans_counter = Counter(mem_intents)

    total = sum(trans_counter.values()) or 1
    ranked = trans_counter.most_common(limit)

    results: list[dict[str, Any]] = []
    for idx, (intent, count) in enumerate(ranked):
        # confidence floor/ceiling prevents overclaiming with sparse data
        confidence = max(0.35, min(0.92, count / total + (0.08 if idx == 0 else 0.0)))
        horizon = "now" if idx == 0 else "next_30m"
        results.append(
            {
                "predicted_intent": intent,
                "confidence": round(confidence, 3),
                "horizon": horizon,
                "evidence": {
                    "signal_intent": signal_intent,
                    "count": count,
                    "sample_size": total,
                },
            }
        )

    # final fallback
    if not results:
        results = [
            {
                "predicted_intent": "status_check",
                "confidence": 0.4,
                "horizon": "now",
                "evidence": {"reason": "cold_start"},
            }
        ]

    return results


def build_proactive_drafts(predictions: list[dict[str, Any]], signal: str) -> list[dict[str, str]]:
    """Generate reversible proactive drafts from predicted intents.

    These are suggestions/preparatory actions, not irreversible execution.
    """
    drafts: list[dict[str, str]] = []
    for p in predictions:
        intent = p.get("predicted_intent", "general_followup")
        confidence = p.get("confidence", 0.0)

        if intent == "status_check":
            action = "Preload service health checks, latest deploy status, and recent error logs."
        elif intent == "remediation":
            action = "Draft root-cause summary + patch plan + rollback checklist for likely failing component."
        elif intent == "deploy":
            action = "Prepare deployment readiness report (tests, migrations, env parity, rollback command)."
        elif intent == "planning":
            action = "Draft a phased implementation plan with owners, milestones, and risk gates."
        elif intent == "summary":
            action = "Assemble a concise DID/NEXT/NEED summary from recent events and memory."
        else:
            action = "Prepare top-3 likely follow-up options with recommended default."

        drafts.append(
            {
                "intent": intent,
                "confidence": f"{confidence:.3f}",
                "proactive_draft": action,
                "trigger_signal": signal,
            }
        )
    return drafts
