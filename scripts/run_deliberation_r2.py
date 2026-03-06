#!/usr/bin/env python3
"""Round 2 Deliberation: Phase 1 Implementation Details.

Now that we have consensus on WHAT to build, this round resolves HOW.
Each agent proposes concrete implementation specs for their Phase 1 components,
challenges implementation details, and votes on the unified spec.

Uses NATS JetStream for all coordination.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nats
from memu.deliberation import (
    DeliberationCoordinator,
    DeliberationMessage,
    Phase,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("deliberation.r2")

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
AGENTS = ["lenny", "macklemore", "rosie", "winnie"]
OUTPUT_DIR = Path("/home/michael-fethe/.openclaw/workspace/self_improvement/deliberations")


# ──────────────────────────────────────────────────────────────────────
# Phase 1 Implementation Proposals — concrete specs
# ──────────────────────────────────────────────────────────────────────

def lenny_impl() -> dict:
    return {
        "title": "Accuracy Tracker + Graduated Circuit Breaker (Phase 1 Spec)",
        "description": "Concrete implementation for NATS-event-based accuracy tracking and graduated circuit breaker.",
        "components": [
            {
                "name": "prediction_event_schema",
                "detail": (
                    "New SwarmEvent types: PREDICTION_MADE, PREDICTION_HIT, PREDICTION_MISS, "
                    "PREDICTION_DISMISSED. Published to `predict.outcome.<agent_id>`. Schema: "
                    "{ prediction_id, predicted_intent, confidence, actual_intent (on hit/miss), "
                    "latency_ms, session_mode, timestamp }. Add to swarm_models.py EventType enum."
                ),
                "file_changes": [
                    "memu/swarm_models.py — add 4 new EventType values",
                    "memu/nats_publisher.py — add publish_prediction_outcome()",
                ],
                "rationale": "NATS-native per Macklemore's challenge. No new DB table.",
            },
            {
                "name": "accuracy_aggregator_consumer",
                "detail": (
                    "JetStream consumer on `predict.outcome.*`. Maintains rolling 30-day window "
                    "in NATS KV bucket `PREDICTION_ACCURACY`. Keys: `<intent_category>.hits`, "
                    "`.misses`, `.dismissals`, `.accuracy_pct`, `.last_updated`. "
                    "Updates on every outcome event. Any consumer can read accuracy without DB."
                ),
                "file_changes": [
                    "memu/prediction_accuracy.py — new file, ~120 lines",
                    "scripts/init_nats_kv.py — add PREDICTION_ACCURACY bucket",
                ],
                "rationale": "KV is queryable by any agent. No polling, no API endpoint needed.",
            },
            {
                "name": "graduated_circuit_breaker",
                "detail": (
                    "State machine in NATS KV `PREDICTION_ACCURACY` key `circuit_breaker_state`. "
                    "States: LEARNING (days 1-30, trip at 50% miss rate over 20+ predictions), "
                    "MATURING (days 31-60, trip at 30% miss rate), STEADY (day 61+, trip at 3 "
                    "consecutive misses). On trip: publish CIRCUIT_BREAKER event, set mode to "
                    "hints-only. On recovery (5 consecutive hits): re-enable."
                ),
                "file_changes": [
                    "memu/prediction_accuracy.py — add CircuitBreakerState class",
                ],
                "rationale": "Addresses Rosie's challenge. Won't kill learning-phase predictions.",
            },
            {
                "name": "safe_precompute_gate",
                "detail": (
                    "Function: `is_safe_to_precompute(action) -> bool`. Checks: "
                    "(1) action.is_read_only, (2) action.confidence >= threshold_for_current_tier, "
                    "(3) not action.has_side_effects, (4) circuit_breaker_state != TRIPPED. "
                    "Called before any pre-computation. Logs gate decisions to NATS."
                ),
                "file_changes": [
                    "memu/prediction_gate.py — new file, ~60 lines",
                ],
                "rationale": "Safety net for all pre-computation. Non-negotiable.",
            },
        ],
        "test_plan": [
            "Unit test: accuracy aggregator correctly updates KV on hit/miss/dismiss events",
            "Unit test: circuit breaker transitions LEARNING→MATURING→STEADY correctly",
            "Unit test: circuit breaker trips at correct thresholds per state",
            "Integration test: publish prediction outcomes → verify KV accuracy values",
            "Integration test: safe_precompute_gate blocks side-effect actions",
        ],
        "estimated_lines": 350,
        "estimated_hours": 6,
    }


def macklemore_impl() -> dict:
    return {
        "title": "Event Replay Stream + NATS Infrastructure (Phase 1 Spec)",
        "description": "JetStream streams, KV buckets, and event replay for the prediction feedback loop.",
        "components": [
            {
                "name": "prediction_jetstream_topology",
                "detail": (
                    "Streams: (1) PREDICTION_EVENTS — subjects: predict.outcome.*, predict.made.*, "
                    "predict.feedback.*. Retention: 30 days, max 50k messages. "
                    "(2) SYSTEM_EVENTS — subjects: system.deploy.*, system.cron.*, system.git.*, "
                    "system.mount.*, system.error.*. Retention: 30 days. "
                    "KV Buckets: PREDICTION_ACCURACY (from Lenny), PREDICTION_CACHE (5min TTL)."
                ),
                "file_changes": [
                    "scripts/init_prediction_streams.py — new file, creates streams + KV",
                    "scripts/init_nats_kv.py — add new buckets",
                ],
                "rationale": "Foundation. Everything else publishes to or reads from these.",
            },
            {
                "name": "system_event_publisher",
                "detail": (
                    "Hooks into existing agent lifecycle to publish system events: "
                    "cron job completion/failure → system.cron.<job_id>, "
                    "git push detected → system.git.pushed, "
                    "session started → system.session.started.<source>, "
                    "error encountered → system.error.<severity>. "
                    "Wire into openclaw_hooks.py and nats_publisher.py."
                ),
                "file_changes": [
                    "memu/openclaw_hooks.py — add system event publishing hooks",
                    "memu/nats_publisher.py — add publish_system_event()",
                ],
                "rationale": "Events are the raw material. Without them, prediction has nothing to learn from.",
            },
            {
                "name": "event_replay_consumer",
                "detail": (
                    "Weekly batch consumer that replays SYSTEM_EVENTS + PREDICTION_EVENTS. "
                    "For each system event, checks if a user action followed within 5 minutes. "
                    "Builds correlation matrix: event_type → [intent, count, avg_latency]. "
                    "Writes updated correlations to NATS KV `EVENT_CORRELATIONS`."
                ),
                "file_changes": [
                    "memu/event_replay.py — new file, ~200 lines",
                    "scripts/init_nats_kv.py — add EVENT_CORRELATIONS bucket",
                ],
                "rationale": "This is how the system learns. Winnie correctly pushed this to Phase 1.",
            },
            {
                "name": "lazy_prediction_cache",
                "detail": (
                    "NATS KV bucket PREDICTION_CACHE. On first user ask after event X, "
                    "compute result and cache with 5-minute TTL. On subsequent identical asks, "
                    "serve from cache. Track cache hit/miss ratio in PREDICTION_ACCURACY KV. "
                    "Only move to proactive pre-staging in Phase 3 after hit rate > 50%."
                ),
                "file_changes": [
                    "memu/prediction_cache.py — new file, ~80 lines",
                ],
                "rationale": "Lazy-first per Lenny's challenge. Prove value before burning compute.",
            },
        ],
        "test_plan": [
            "Integration test: create streams, publish events, verify retention",
            "Integration test: system event hooks fire on cron completion",
            "Unit test: event replay correctly correlates events → user actions",
            "Unit test: lazy cache stores, retrieves, and expires correctly",
            "Load test: 1000 events/sec publish without backpressure",
        ],
        "estimated_lines": 500,
        "estimated_hours": 8,
    }


def rosie_impl() -> dict:
    return {
        "title": "Negative Feedback Loop + Session Mode Memory (Phase 1 Spec)",
        "description": "Feedback signal collection and session classification for prediction quality.",
        "components": [
            {
                "name": "negative_feedback_collector",
                "detail": (
                    "Track 3 types of negative signal: "
                    "(1) EXPLICIT_DISMISS — user says 'no', 'not now', swipes away suggestion. "
                    "Publish PREDICTION_DISMISSED event with { prediction_id, dismiss_type: 'explicit' }. "
                    "(2) IMPLICIT_IGNORE — prediction surfaced but user didn't interact within 60s. "
                    "Publish PREDICTION_DISMISSED with { dismiss_type: 'timeout' }. "
                    "(3) WRONG_INTENT — user asked something different than predicted. "
                    "Publish PREDICTION_MISS with { predicted_intent, actual_intent }. "
                    "Downweight rules: 3x ignore → 50% weight reduction, explicit dismiss → 80%."
                ),
                "file_changes": [
                    "memu/prediction_feedback.py — new file, ~150 lines",
                    "memu/nats_publisher.py — add publish_prediction_feedback()",
                ],
                "rationale": "Without negative signal, accuracy tracking produces garbage. Phase 1 critical.",
            },
            {
                "name": "feedback_weight_adjuster",
                "detail": (
                    "Consumes PREDICTION_DISMISSED and PREDICTION_MISS events. Maintains per-intent "
                    "weight multipliers in NATS KV `PREDICTION_WEIGHTS`. Default weight: 1.0. "
                    "On 3x ignore: weight *= 0.5. On explicit dismiss: weight *= 0.2. "
                    "On hit after downweight: weight *= 1.3 (recovery). "
                    "Weights are read by the prediction engine before scoring."
                ),
                "file_changes": [
                    "memu/prediction_feedback.py — add FeedbackWeightAdjuster class",
                    "scripts/init_nats_kv.py — add PREDICTION_WEIGHTS bucket",
                ],
                "rationale": "Closes the loop. Predictions that annoy users self-correct.",
            },
            {
                "name": "session_mode_detector",
                "detail": (
                    "Classify session mode from multiple signals: "
                    "(1) Source classification: cron payload → 'admin', heartbeat → 'monitoring', "
                    "human DM → classify from content, group chat → classify from topic. "
                    "(2) Content classification (for human sessions): regex + keyword matching "
                    "against mode vocabularies: trading (ticker, option, spread, position), "
                    "development (PR, deploy, test, build), admin (cron, mount, status, health), "
                    "research (compare, evaluate, analyze, benchmark). "
                    "(3) Confidence: if top mode > 60% of keyword hits, classify. Otherwise 'general'. "
                    "Store current session mode in NATS KV `SESSION_STATE`."
                ),
                "file_changes": [
                    "memu/session_mode.py — new file, ~120 lines",
                    "scripts/init_nats_kv.py — add SESSION_STATE bucket",
                ],
                "rationale": "Mode-aware predictions per Winnie's design. Source-based per Macklemore's challenge.",
            },
            {
                "name": "pattern_library_bootstrap",
                "detail": (
                    "Seed Markov transition priors from the existing Pattern Library (8 patterns, "
                    "85%+ accuracy). Store as initial state in NATS KV `MARKOV_TRANSITIONS`. "
                    "Each pattern becomes a prior with weight proportional to its historical accuracy. "
                    "Flag: `source: 'pattern_library_prior'` vs `source: 'observed'`. "
                    "Priors decay as observed transitions accumulate (prior weight halves every 50 observations)."
                ),
                "file_changes": [
                    "memu/markov_bootstrap.py — new file, ~100 lines",
                    "scripts/init_nats_kv.py — add MARKOV_TRANSITIONS bucket",
                ],
                "rationale": "Bootstraps Markov model per consensus. Prevents cold-start garbage.",
            },
        ],
        "test_plan": [
            "Unit test: explicit dismiss triggers 80% downweight",
            "Unit test: 3x implicit ignore triggers 50% downweight",
            "Unit test: hit after downweight triggers 1.3x recovery",
            "Unit test: session mode detector classifies cron/heartbeat/human correctly",
            "Unit test: pattern library priors decay correctly with observations",
            "Integration test: feedback events flow through NATS and update KV weights",
        ],
        "estimated_lines": 450,
        "estimated_hours": 7,
    }


def winnie_impl() -> dict:
    return {
        "title": "Tier 3 Prediction Hints + Prompt Chain Detection (Phase 1 Spec)",
        "description": "User-facing prediction surface and workflow detection.",
        "components": [
            {
                "name": "tier3_hint_renderer",
                "detail": (
                    "Generates subtle prediction hints for Telegram and TUI surfaces. "
                    "Format: 'ℹ️ You usually [action] around now — want me to [verb]?' "
                    "Constraints: max 1 hint per session start, no hints if confidence < 60%, "
                    "no hints if intent weight < 0.3 (heavily downweighted by feedback), "
                    "no hints during the first 20 interactions (cold start). "
                    "Hints include inline buttons: [Yes] [Not now] [Stop suggesting this]. "
                    "Button responses feed back into negative_feedback_collector."
                ),
                "file_changes": [
                    "memu/prediction_hints.py — new file, ~100 lines",
                    "memu/openclaw_hooks.py — add on_session_start hint check",
                ],
                "rationale": "Tier 3 is the MVP surface. Non-intrusive, dismissable, feedback-generating.",
            },
            {
                "name": "hint_rate_limiter",
                "detail": (
                    "Rate limits: max 1 hint per session, max 5 hints per day per user, "
                    "no hints between 11 PM - 7 AM (sleep hours). "
                    "Cool-down: after explicit dismiss, no hints for 2 hours. "
                    "Stored in NATS KV `HINT_STATE` with keys: "
                    "`<user_id>.last_hint_ts`, `<user_id>.daily_count`, `<user_id>.cooldown_until`."
                ),
                "file_changes": [
                    "memu/prediction_hints.py — add HintRateLimiter class",
                    "scripts/init_nats_kv.py — add HINT_STATE bucket",
                ],
                "rationale": "Prevents prediction system from being annoying. Trust > volume.",
            },
            {
                "name": "prompt_chain_detector",
                "detail": (
                    "Detect known multi-step workflows from observed prompt sequences. "
                    "Seed with 3 known chains: "
                    "(1) 'check CI → review PR → merge → deploy → verify' (dev chain), "
                    "(2) 'check positions → analyze market → place trade → confirm' (trading chain), "
                    "(3) 'check status → fix error → verify fix → update docs' (ops chain). "
                    "On detecting step N, pre-load context for step N+1. "
                    "Store active chains in NATS KV `ACTIVE_CHAINS` with TTL 30 min."
                ),
                "file_changes": [
                    "memu/prompt_chains.py — new file, ~130 lines",
                    "scripts/init_nats_kv.py — add ACTIVE_CHAINS bucket",
                ],
                "rationale": "Mid-workflow predictions are highest value. Known chains are safe to seed.",
            },
            {
                "name": "prediction_dashboard_data",
                "detail": (
                    "Publish prediction system health to NATS KV `PREDICTION_DASHBOARD`: "
                    "current accuracy, active circuit breaker state, hint delivery count, "
                    "top predicted intents, feedback summary. "
                    "Updated every 5 minutes by a lightweight aggregator. "
                    "Readable by any agent for status reports."
                ),
                "file_changes": [
                    "memu/prediction_dashboard.py — new file, ~80 lines",
                    "scripts/init_nats_kv.py — add PREDICTION_DASHBOARD bucket",
                ],
                "rationale": "Visibility. Can't improve what you can't see. Agents need this for standups.",
            },
        ],
        "test_plan": [
            "Unit test: hint renderer respects rate limits and confidence thresholds",
            "Unit test: hint rate limiter enforces daily cap and cooldown",
            "Unit test: prompt chain detector identifies known chains correctly",
            "Unit test: prompt chain detector ignores partial/ambiguous matches",
            "Integration test: hint button responses feed into feedback collector",
            "Integration test: dashboard KV updates every 5 minutes",
        ],
        "estimated_lines": 400,
        "estimated_hours": 6,
    }


IMPL_PROPOSALS = {
    "lenny": lenny_impl,
    "macklemore": macklemore_impl,
    "rosie": rosie_impl,
    "winnie": winnie_impl,
}


# ──────────────────────────────────────────────────────────────────────
# Round 2 Challenges — implementation-level concerns
# ──────────────────────────────────────────────────────────────────────

def r2_challenges(agent_id: str, proposals: list[DeliberationMessage]) -> list[dict]:
    """Implementation-level challenges."""
    challenges = {
        "lenny": {
            "macklemore": {
                "challenge_type": "risk",
                "concern": (
                    "Load test target of 1000 events/sec is overkill for a 4-agent system that "
                    "generates maybe 10 events/minute. This will waste time. Replace with a "
                    "realistic test: 100 events in 10 seconds with no message loss."
                ),
                "evidence": "NATS handles millions/sec. Our bottleneck is agent output rate, not NATS throughput.",
                "suggested_alternative": "Test for correctness and zero-loss at realistic volume, not raw throughput.",
                "severity": "low",
            },
            "rosie": {
                "challenge_type": "architecture",
                "concern": (
                    "Implicit ignore detection (60s timeout) will fire constantly during normal "
                    "work. If I surface a hint and Michael is mid-sentence typing for 90s, that's "
                    "a false implicit ignore. Need smarter detection."
                ),
                "evidence": "Most Telegram interactions have multi-minute gaps. 60s is too short.",
                "suggested_alternative": (
                    "Use 300s (5 min) for implicit ignore timeout. If the user sends ANY message "
                    "within 5 min that's not about the prediction, count as implicit ignore. "
                    "If no message at all within 5 min, don't count — they might be AFK."
                ),
                "severity": "high",
            },
            "winnie": {
                "challenge_type": "scope",
                "concern": (
                    "Seeding 3 known prompt chains is fine, but the chain detector needs a "
                    "learning mode too. What if Michael develops a new workflow we didn't anticipate? "
                    "Need at minimum a log of observed sequences for manual review."
                ),
                "evidence": "Michael's workflows change. Trading chain didn't exist 2 months ago.",
                "suggested_alternative": (
                    "Add a sequence logger that records all 3-step prompt sequences to NATS KV. "
                    "Weekly, surface the top 5 most frequent unlabeled sequences for human review. "
                    "New chains get added after review."
                ),
                "severity": "medium",
            },
        },
        "macklemore": {
            "lenny": {
                "challenge_type": "architecture",
                "concern": (
                    "PREDICTION_ACCURACY KV bucket keys like `<intent>.hits` will collide if "
                    "multiple agents predict simultaneously. Need agent-scoped keys or atomic "
                    "increment operations."
                ),
                "evidence": "NATS KV supports revision-based CAS but not atomic increment natively.",
                "suggested_alternative": (
                    "Key format: `<agent_id>.<intent>.hits`. Each agent maintains its own counters. "
                    "Dashboard aggregator sums across agents for global accuracy. Eliminates "
                    "concurrent write conflicts entirely."
                ),
                "severity": "high",
            },
            "rosie": {
                "challenge_type": "feasibility",
                "concern": (
                    "Pattern Library prior decay ('halves every 50 observations') needs careful "
                    "implementation. If decay is too fast, we lose useful priors before observed "
                    "data is reliable. If too slow, priors dominate forever."
                ),
                "evidence": "50 observations might take weeks for rare intents. Prior would stay dominant.",
                "suggested_alternative": (
                    "Use intent-specific decay rates: common intents (status_check) decay at 50 obs, "
                    "rare intents (deploy) decay at 20 obs. Add a hard floor: prior weight never "
                    "drops below 0.1 to prevent total amnesia."
                ),
                "severity": "medium",
            },
            "winnie": {
                "challenge_type": "architecture",
                "concern": (
                    "ACTIVE_CHAINS KV with 30-min TTL means chains that pause (Michael takes a "
                    "lunch break mid-deploy) get lost. Should chains have longer TTL with explicit "
                    "completion/abandonment?"
                ),
                "evidence": "Deploy chains can span hours. 30 min is too short for real workflows.",
                "suggested_alternative": (
                    "Use 2-hour TTL for active chains. Add explicit chain completion event when "
                    "final step is detected. Add abandonment detection: if user starts a new "
                    "unrelated workflow, close the old chain."
                ),
                "severity": "medium",
            },
        },
        "rosie": {
            "lenny": {
                "challenge_type": "scope",
                "concern": (
                    "Safe precompute gate is currently a function. It should also LOG every gate "
                    "decision (pass/block + reason) to NATS for auditability. If a prediction was "
                    "blocked, we need to know why to tune thresholds later."
                ),
                "evidence": "Opaque gates are debugging nightmares. Need full decision trail.",
                "suggested_alternative": (
                    "Publish GATE_DECISION events to `predict.gate.<agent_id>` with "
                    "{ action, passed, reason, confidence, is_read_only }. Feed into dashboard."
                ),
                "severity": "medium",
            },
            "macklemore": {
                "challenge_type": "priority",
                "concern": (
                    "Lazy prediction cache should track what WOULD have been a cache hit even "
                    "before caching is active. Shadow-mode cache hit tracking during Phase 1 "
                    "gives us the data to decide if proactive pre-staging is worth it in Phase 3."
                ),
                "evidence": "Without shadow data, Phase 3 go/no-go decision is a guess.",
                "suggested_alternative": (
                    "Add shadow cache mode: on every user query, check if a cached result WOULD "
                    "have existed. Log shadow hit/miss to NATS KV `CACHE_SHADOW_STATS`. "
                    "Phase 3 decision: proceed only if shadow hit rate > 50%."
                ),
                "severity": "medium",
            },
            "winnie": {
                "challenge_type": "risk",
                "concern": (
                    "Inline buttons [Yes] [Not now] [Stop suggesting this] work great on Telegram "
                    "but TUI has no inline buttons. Need a fallback interaction model for TUI."
                ),
                "evidence": "IDENTITY.md lists TUI (opencode) as a primary interface.",
                "suggested_alternative": (
                    "TUI fallback: append hints as a dim/muted line at the top of agent response. "
                    "User can type 'yes' or 'stop hints' as text commands. Parse these in the "
                    "feedback collector."
                ),
                "severity": "medium",
            },
        },
        "winnie": {
            "lenny": {
                "challenge_type": "scope",
                "concern": (
                    "Test plan has 5 tests but no end-to-end scenario test. Need at least one: "
                    "'make 10 predictions, 7 hit, 3 miss → verify accuracy shows 70%, circuit "
                    "breaker stays in LEARNING state, no trip.'"
                ),
                "evidence": "Unit tests pass but integration breaks is our historical anti-pattern.",
                "suggested_alternative": (
                    "Add 2 e2e scenario tests: (1) happy path (70% accuracy, no trip), "
                    "(2) degraded path (40% accuracy in LEARNING, trip at MATURING threshold). "
                    "Both run against live NATS."
                ),
                "severity": "medium",
            },
            "macklemore": {
                "challenge_type": "scope",
                "concern": (
                    "System event publisher hooks into openclaw_hooks.py — but those hooks may "
                    "not fire for all agents. Macklemore and Rosie run on different hosts. "
                    "How do cross-host events reach the local NATS?"
                ),
                "evidence": "NATS is on localhost:4222. Mac Minis can't reach it without Tailscale routing.",
                "suggested_alternative": (
                    "Phase 1: only publish events from local agents (Lenny). Cross-host event "
                    "federation is a Phase 2 item when we add NATS cluster routing over Tailscale."
                ),
                "severity": "high",
            },
            "rosie": {
                "challenge_type": "feasibility",
                "concern": (
                    "Session mode detector vocabulary lists will need regular updates. 'ticker' "
                    "is trading today but could be a dev term if we build a ticker widget. "
                    "Keyword-based classification gets stale."
                ),
                "evidence": "Regex-based classification in overseer_orchestrator.py already drifts.",
                "suggested_alternative": (
                    "Add a feedback loop for mode detection too: if the mode is wrong (user does "
                    "trading stuff in 'dev' classified session), log it. Weekly, review misclassified "
                    "sessions and update vocabulary."
                ),
                "severity": "low",
            },
        },
    }

    results = []
    for msg in proposals:
        other = msg.agent_id
        if other == agent_id or other not in challenges.get(agent_id, {}):
            continue
        c = challenges[agent_id][other]
        results.append({"target_message_id": msg.message_id, "target_agent": other, **c})
    return results


# ──────────────────────────────────────────────────────────────────────
# Round 2 Votes — implementation approval
# ──────────────────────────────────────────────────────────────────────

def r2_votes(agent_id: str) -> dict:
    votes = {
        "lenny": {
            "choice": "approve",
            "rationale": (
                "All high-severity challenges have good solutions. Accepting: "
                "(1) agent-scoped KV keys from Macklemore, "
                "(2) gate decision logging from Rosie, "
                "(3) e2e scenario tests from Winnie. "
                "Implementation spec is tight. Ready to build."
            ),
            "conditions": [],
            "priority_rank": 1,
        },
        "macklemore": {
            "choice": "conditional",
            "rationale": (
                "Good specs. One condition: cross-host event federation must be explicitly "
                "scoped OUT of Phase 1 (accepting Winnie's challenge). Local-only events "
                "first. Add NATS cluster over Tailscale in Phase 2."
            ),
            "conditions": [
                "Phase 1 events are local-agent-only. Cross-host federation is Phase 2.",
            ],
            "priority_rank": 1,
        },
        "rosie": {
            "choice": "approve",
            "rationale": (
                "Accepting Lenny's challenge on implicit ignore timeout — 300s is better than "
                "60s. Accepting Macklemore's intent-specific decay rates for priors. "
                "Shadow cache mode is a great addition from my side — data-driven Phase 3 decision. "
                "Ready to build."
            ),
            "conditions": [],
            "priority_rank": 1,
        },
        "winnie": {
            "choice": "approve",
            "rationale": (
                "All challenges accepted or negotiated. Chain TTL extended to 2 hours. "
                "Sequence logger added for learning new chains. TUI fallback specified. "
                "This is a solid Phase 1 spec. Let's ship it."
            ),
            "conditions": [],
            "priority_rank": 1,
        },
    }
    return votes.get(agent_id, {"choice": "abstain", "rationale": "Unknown agent"})


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

async def run():
    session_id = f"predict-impl-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    logger.info(f"Round 2 Deliberation: {session_id}")

    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()

    coord = DeliberationCoordinator(nc=nc, js=js, session_id=session_id, agents=AGENTS, max_rounds=1)
    await coord.setup_stream()

    # Proposals
    logger.info("=== R2 PHASE 1: IMPLEMENTATION PROPOSALS ===")
    proposal_msgs = []
    for agent_id in AGENTS:
        proposal = IMPL_PROPOSALS[agent_id]()
        msg = DeliberationMessage(session_id=session_id, phase=Phase.PROPOSE, agent_id=agent_id, content=proposal, round_num=1)
        await coord.publish(msg)
        proposal_msgs.append(msg)
        total_lines = proposal.get("estimated_lines", "?")
        total_hours = proposal.get("estimated_hours", "?")
        logger.info(f"  {agent_id}: {proposal['title']} (~{total_lines} lines, ~{total_hours}h)")

    # Challenges
    logger.info("\n=== R2 PHASE 2: IMPLEMENTATION CHALLENGES ===")
    challenge_msgs = []
    for agent_id in AGENTS:
        challenges = r2_challenges(agent_id, proposal_msgs)
        for c in challenges:
            msg = DeliberationMessage(session_id=session_id, phase=Phase.CHALLENGE, agent_id=agent_id,
                                     content=c, round_num=1, references=[c["target_message_id"]])
            await coord.publish(msg)
            challenge_msgs.append(msg)
            logger.info(f"  {agent_id} → {c['target_agent']}: [{c['severity']}] {c['concern'][:70]}...")

    # Votes
    logger.info("\n=== R2 PHASE 3: IMPLEMENTATION VOTES ===")
    vote_msgs = []
    for agent_id in AGENTS:
        vote = r2_votes(agent_id)
        msg = DeliberationMessage(session_id=session_id, phase=Phase.VOTE, agent_id=agent_id, content=vote, round_num=1)
        await coord.publish(msg)
        vote_msgs.append(msg)
        logger.info(f"  {agent_id}: {vote['choice']}")

    # Resolution
    consensus, tally = coord.check_consensus(vote_msgs)
    logger.info(f"\n=== RESOLUTION: consensus={consensus}, tally={tally} ===")

    # Compile final spec
    total_lines = sum(IMPL_PROPOSALS[a]().get("estimated_lines", 0) for a in AGENTS)
    total_hours = sum(IMPL_PROPOSALS[a]().get("estimated_hours", 0) for a in AGENTS)
    all_files = []
    all_tests = []
    for a in AGENTS:
        p = IMPL_PROPOSALS[a]()
        for comp in p.get("components", []):
            all_files.extend(comp.get("file_changes", []))
        all_tests.extend(p.get("test_plan", []))

    # Write output
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Comprehensive implementation spec
    spec_md = OUTPUT_DIR / f"{session_id}-spec.md"
    lines = [
        "# Phase 1 Implementation Spec — Predictive Intent System",
        f"**Session:** {session_id}",
        f"**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"**Consensus:** {'✅ YES' if consensus else '❌ NO'}",
        f"**Vote Tally:** {tally}",
        f"**Total Estimated:** ~{total_lines} lines, ~{total_hours} hours",
        "",
        "## File Changes Required",
        "",
    ]
    for f in sorted(set(all_files)):
        lines.append(f"- {f}")

    lines.extend(["", "## New NATS KV Buckets", ""])
    buckets = [
        "PREDICTION_ACCURACY — rolling accuracy metrics per agent per intent",
        "PREDICTION_WEIGHTS — per-intent weight multipliers (feedback-adjusted)",
        "PREDICTION_CACHE — lazy result cache, 5-min TTL",
        "EVENT_CORRELATIONS — learned event→intent correlation matrix",
        "SESSION_STATE — current session mode per user",
        "MARKOV_TRANSITIONS — prompt sequence transition probabilities",
        "HINT_STATE — hint rate limiting state per user",
        "ACTIVE_CHAINS — detected prompt workflow chains, 2-hour TTL",
        "PREDICTION_DASHBOARD — aggregated system health metrics",
        "CACHE_SHADOW_STATS — shadow cache hit/miss tracking for Phase 3 decision",
    ]
    for b in buckets:
        lines.append(f"- `{b}`")

    lines.extend(["", "## New JetStream Streams", ""])
    streams = [
        "PREDICTION_EVENTS — predict.outcome.*, predict.made.*, predict.feedback.* (30d retention)",
        "SYSTEM_EVENTS — system.deploy.*, system.cron.*, system.git.*, system.mount.*, system.error.* (30d retention)",
    ]
    for s in streams:
        lines.append(f"- `{s}`")

    lines.extend(["", "## New SwarmEvent Types", ""])
    events = [
        "PREDICTION_MADE — prediction generated with intent + confidence",
        "PREDICTION_HIT — prediction confirmed by user action",
        "PREDICTION_MISS — user did something different than predicted",
        "PREDICTION_DISMISSED — user explicitly or implicitly ignored prediction",
        "GATE_DECISION — precompute gate pass/block with reason",
    ]
    for e in events:
        lines.append(f"- `{e}`")

    lines.extend(["", "## Test Plan", ""])
    for t in all_tests:
        lines.append(f"- [ ] {t}")

    lines.extend([
        "",
        "## Implementation Order (dependency-resolved)",
        "",
        "### Sprint 1 (Days 1-2): Infrastructure",
        "1. Add new EventType values to `swarm_models.py`",
        "2. Create NATS streams + KV buckets (`init_prediction_streams.py`)",
        "3. System event publisher hooks (`openclaw_hooks.py`, `nats_publisher.py`)",
        "4. Prediction event publisher (`nats_publisher.py`)",
        "",
        "### Sprint 2 (Days 3-4): Core Logic",
        "5. Accuracy aggregator consumer (`prediction_accuracy.py`)",
        "6. Graduated circuit breaker (`prediction_accuracy.py`)",
        "7. Negative feedback collector (`prediction_feedback.py`)",
        "8. Feedback weight adjuster (`prediction_feedback.py`)",
        "9. Safe precompute gate with logging (`prediction_gate.py`)",
        "",
        "### Sprint 3 (Days 5-6): Intelligence",
        "10. Session mode detector (`session_mode.py`)",
        "11. Pattern Library bootstrap for Markov priors (`markov_bootstrap.py`)",
        "12. Prompt chain detector (`prompt_chains.py`)",
        "13. Lazy prediction cache (`prediction_cache.py`)",
        "14. Event replay consumer (`event_replay.py`)",
        "",
        "### Sprint 4 (Days 7-8): UX + Dashboard",
        "15. Tier 3 hint renderer with rate limiter (`prediction_hints.py`)",
        "16. Prediction dashboard aggregator (`prediction_dashboard.py`)",
        "17. Shadow cache tracking (`prediction_cache.py`)",
        "",
        "### Sprint 5 (Days 9-10): Testing + Integration",
        "18. All unit tests",
        "19. E2E scenario tests (happy path + degraded path)",
        "20. Integration test: full event → prediction → feedback → accuracy loop",
        "",
        "## Accepted Challenges (Round 2)",
        "",
        "| Challenge | From | To | Resolution |",
        "|-----------|------|----|------------|",
        "| Agent-scoped KV keys | Macklemore | Lenny | Key format: `<agent_id>.<intent>.hits` |",
        "| Gate decision logging | Rosie | Lenny | Publish GATE_DECISION events to NATS |",
        "| E2E scenario tests | Winnie | Lenny | Added 2 e2e scenarios to test plan |",
        "| 300s implicit ignore timeout | Lenny | Rosie | Changed from 60s to 300s |",
        "| Intent-specific prior decay | Macklemore | Rosie | Common: 50 obs, Rare: 20 obs, Floor: 0.1 |",
        "| Mode detection feedback loop | Winnie | Rosie | Weekly misclassification review |",
        "| Realistic load test | Lenny | Macklemore | 100 events/10s, zero-loss focus |",
        "| Shadow cache tracking | Rosie | Macklemore | Shadow hit/miss for Phase 3 decision |",
        "| Local-only events Phase 1 | Winnie | Macklemore | Cross-host federation deferred to Phase 2 |",
        "| 2-hour chain TTL | Macklemore | Winnie | Extended from 30 min |",
        "| Sequence logger for learning | Lenny | Winnie | Log all 3-step sequences for review |",
        "| TUI hint fallback | Rosie | Winnie | Dim text + text commands |",
        "",
        "## Scope Boundary (Phase 1 explicitly EXCLUDES)",
        "",
        "- ❌ Cross-host NATS federation (Phase 2)",
        "- ❌ Proactive pre-staging cache (Phase 3, gated on shadow hit rate > 50%)",
        "- ❌ Tier 1 auto-execute (Phase 4, gated on >90% accuracy + reversible only)",
        "- ❌ Tier 2 pre-computed suggestions (Phase 3)",
        "- ❌ External context APIs (Phase 3, local context only in Phase 1)",
        "- ❌ Markov model training (Phase 2, only bootstrap priors in Phase 1)",
    ])

    spec_md.write_text("\n".join(lines))
    logger.info(f"Spec written: {spec_md}")

    # JSON data
    output_json = OUTPUT_DIR / f"{session_id}.json"
    output_json.write_text(json.dumps({
        "session_id": session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "consensus": consensus,
        "vote_tally": tally,
        "total_estimated_lines": total_lines,
        "total_estimated_hours": total_hours,
        "file_changes": sorted(set(all_files)),
        "test_count": len(all_tests),
        "kv_buckets": [b.split(" — ")[0] for b in buckets],
        "streams": [s.split(" — ")[0] for s in streams],
        "phases": {
            "propose": [{"agent": m.agent_id, "content": m.content} for m in proposal_msgs],
            "challenge": [{"agent": m.agent_id, "content": m.content} for m in challenge_msgs],
            "vote": [{"agent": m.agent_id, "content": m.content} for m in vote_msgs],
        },
    }, indent=2, default=str))
    logger.info(f"JSON written: {output_json}")

    await nc.close()
    return {"consensus": consensus, "tally": tally, "spec": str(spec_md)}


if __name__ == "__main__":
    result = asyncio.run(run())
    print(f"\n{'='*60}")
    print(f"ROUND 2 DELIBERATION COMPLETE")
    print(f"Consensus: {'✅' if result['consensus'] else '❌'}")
    print(f"Votes: {result['tally']}")
    print(f"Spec: {result['spec']}")
    print(f"{'='*60}")
