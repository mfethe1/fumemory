#!/usr/bin/env python3
"""Run a multi-agent deliberation session on the Predictive Intent System.

Blends all 4 ideas:
  1. Temporal pattern mining (Rosie's analytics strength)
  2. Prompt sequence modeling (Winnie's product/UX angle)
  3. Context-triggered predictions (Macklemore's infra/event-driven perspective)
  4. Active pre-computation (Lenny's QA/verification gate)

Uses NATS JetStream for real-time propose → challenge → refine → vote loops.

Usage:
    python3 scripts/run_deliberation.py [--rounds 3] [--timeout 30]
"""

from __future__ import annotations

import asyncio
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# Add parent to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nats
from memu.deliberation import (
    DeliberationCoordinator,
    DeliberationMessage,
    Phase,
    VoteChoice,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("deliberation.runner")

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
AGENTS = ["lenny", "macklemore", "rosie", "winnie"]
OUTPUT_DIR = Path("/home/michael-fethe/.openclaw/workspace/self_improvement/deliberations")


# ──────────────────────────────────────────────────────────────────────
# Agent Proposals — each agent's perspective on the predictive system
# ──────────────────────────────────────────────────────────────────────

def lenny_proposal() -> dict:
    """QA/Contract: verification gates, accuracy tracking, safe pre-computation."""
    return {
        "title": "Verified Predictive Execution with Safety Gates",
        "description": (
            "Build prediction pipeline with mandatory verification at every stage. "
            "Predictions only auto-execute if confidence > threshold AND the action is "
            "reversible. Track accuracy obsessively — any prediction path dropping below "
            "70% accuracy gets auto-disabled."
        ),
        "components": [
            {
                "name": "prediction_accuracy_tracker",
                "detail": (
                    "Record every prediction (intent, confidence, timestamp). After user's actual "
                    "next action, score the prediction as hit/miss. Maintain rolling 30-day accuracy "
                    "per intent category. Publish accuracy metrics to NATS for team visibility."
                ),
                "rationale": "Can't improve what we don't measure. Current accuracy is 0% (0 executed).",
            },
            {
                "name": "safe_precompute_gate",
                "detail": (
                    "Pre-computation runs ONLY for reversible, read-only operations (status checks, "
                    "file reads, search preloads). Write operations require explicit user trigger. "
                    "Gate checks: is_reversible AND confidence >= 0.80 AND no_side_effects."
                ),
                "rationale": "Pre-computing a deploy or a message send is dangerous. Read-only is safe.",
            },
            {
                "name": "cold_start_bootstrapper",
                "detail": (
                    "For new users/sessions with no history, use the Pattern Library (8 patterns, "
                    "85%+ accuracy) as priors. Transition to personalized predictions after 20 "
                    "observed interactions."
                ),
                "rationale": "Avoid garbage predictions from sparse data. Bootstrap from proven patterns.",
            },
            {
                "name": "prediction_circuit_breaker",
                "detail": (
                    "If 3 consecutive predictions miss, pause auto-execution and fall back to "
                    "suggestion-only mode. Resume after manual review or accuracy recovery."
                ),
                "rationale": "Prevents runaway bad predictions from eroding trust.",
            },
        ],
        "risks": [
            "Over-conservative gates may make the system feel slow/unhelpful",
            "Accuracy tracking adds latency to every interaction",
            "Cold start bootstrapper may create false confidence from pattern priors",
        ],
        "dependencies": ["memU operational for history storage", "NATS for accuracy event publishing"],
        "estimated_effort": "days",
        "confidence": 0.88,
    }


def rosie_proposal() -> dict:
    """Research/Analyst: temporal pattern mining, behavioral analytics."""
    return {
        "title": "Temporal Behavioral Intelligence Engine",
        "description": (
            "Mine time-of-day, day-of-week, and sequence patterns from user interaction history. "
            "Use statistical models (not just regex) to identify recurring behavioral cycles. "
            "Enrich predictions with external context (market hours, meeting schedules, deploy cycles)."
        ),
        "components": [
            {
                "name": "temporal_pattern_miner",
                "detail": (
                    "Analyze interaction timestamps to find recurring patterns: "
                    "'Michael checks trading setup at 8:30 AM weekdays', "
                    "'deployment status checks spike after PR merges', "
                    "'infrastructure questions cluster on Mondays'. "
                    "Use sliding window + frequency analysis, not just raw counts."
                ),
                "rationale": "Time is the strongest predictor of routine behavior. Currently unused.",
            },
            {
                "name": "sequence_markov_model",
                "detail": (
                    "Build a Markov chain from prompt→prompt transitions. Track 2-gram and 3-gram "
                    "sequences: 'after asking about deploy status, user asks about error logs 72% "
                    "of the time'. Update transition probabilities with exponential recency weighting."
                ),
                "rationale": "next_intent.py does basic intent classification but no sequence modeling.",
            },
            {
                "name": "external_context_enricher",
                "detail": (
                    "Pull contextual signals: market open/close times, git push events, cron job "
                    "completions, calendar events. Weight predictions higher when external context "
                    "aligns (e.g., predict trading questions near market open)."
                ),
                "rationale": "Predictions without external context miss the 'why now' signal.",
            },
            {
                "name": "behavioral_cohort_analysis",
                "detail": (
                    "Cluster interaction sessions by purpose (dev sprint, trading session, admin "
                    "maintenance, research deep-dive). Once session type is identified, load the "
                    "cohort-specific prediction model."
                ),
                "rationale": "Michael in 'trading mode' has different next-asks than 'dev mode'.",
            },
        ],
        "risks": [
            "Temporal patterns may overfit to recent habits that change",
            "Markov chains need 100+ transitions per state for reliability",
            "External context APIs add latency and failure points",
        ],
        "dependencies": [
            "memU search_history with timestamps",
            "Git webhook or polling for push events",
            "Calendar API access (optional)",
        ],
        "estimated_effort": "days",
        "confidence": 0.82,
    }


def macklemore_proposal() -> dict:
    """Infra/Platform: event-driven triggers, NATS-native architecture."""
    return {
        "title": "Event-Driven Prediction Mesh on NATS",
        "description": (
            "Make predictions event-driven, not poll-driven. Every system event (deploy, "
            "cron completion, error, git push, mount change) publishes to NATS. A prediction "
            "engine subscribes to these events and fires context-triggered predictions in "
            "real-time. Pre-stage results before the user even opens a chat."
        ),
        "components": [
            {
                "name": "event_taxonomy_registry",
                "detail": (
                    "Define a structured taxonomy of triggering events: "
                    "deploy.completed, cron.failed, git.pushed, mount.changed, "
                    "market.opened, error.production, session.started. "
                    "Each event type maps to a set of likely follow-up intents with weights."
                ),
                "rationale": "Without structured events, predictions are just guessing. Events are facts.",
            },
            {
                "name": "nats_prediction_consumer",
                "detail": (
                    "JetStream consumer that watches system event subjects. On each event, "
                    "looks up the event→intent mapping, scores against recent context, and "
                    "publishes predicted intents to `predict.<agent_id>.next`. Agents subscribe "
                    "and pre-stage relevant data."
                ),
                "rationale": "NATS is already running. This is a consumer, not a new service.",
            },
            {
                "name": "pre_stage_cache",
                "detail": (
                    "NATS KV bucket `PREDICTION_CACHE` stores pre-computed results: "
                    "health check outputs, recent error summaries, deploy diffs, trading "
                    "snapshots. TTL: 5 minutes. When user asks, serve from cache instead "
                    "of computing fresh."
                ),
                "rationale": "Instant responses feel magical. 5-min TTL prevents staleness.",
            },
            {
                "name": "event_replay_for_training",
                "detail": (
                    "Persist all events to a JetStream stream with 30-day retention. "
                    "Replay events to retrain prediction weights weekly. This is how the "
                    "system learns which events actually predict which user actions."
                ),
                "rationale": "Historical replay is how you close the feedback loop.",
            },
        ],
        "risks": [
            "Event taxonomy requires maintenance as system evolves",
            "Pre-stage cache may compute things nobody asks for (waste)",
            "NATS KV has size limits — cache values must be compact",
        ],
        "dependencies": [
            "NATS JetStream (operational ✅)",
            "System events published to NATS (partially done via nats_publisher.py)",
            "KV bucket creation (init_nats_kv.py exists)",
        ],
        "estimated_effort": "days",
        "confidence": 0.85,
    }


def winnie_proposal() -> dict:
    """Product/UX: prompt sequence modeling, user experience, progressive disclosure."""
    return {
        "title": "Anticipatory UX with Progressive Prediction Disclosure",
        "description": (
            "Focus on the user experience of predictions. Bad predictions erode trust faster "
            "than no predictions. Use progressive disclosure: start with subtle hints "
            "('I noticed you usually check X around now'), graduate to pre-loaded results, "
            "then to auto-execution — only after proving accuracy."
        ),
        "components": [
            {
                "name": "prediction_confidence_tiers",
                "detail": (
                    "Tier 1 (>90% confidence): Auto-execute + show result inline. "
                    "Tier 2 (75-90%): Pre-compute + offer as suggestion ('Ready when you need it'). "
                    "Tier 3 (60-75%): Subtle hint only ('You might want to check...'). "
                    "Tier 4 (<60%): Silent — learn but don't surface."
                ),
                "rationale": "Users hate wrong auto-actions. Progressive disclosure builds trust.",
            },
            {
                "name": "prompt_chain_detector",
                "detail": (
                    "Detect multi-step workflows: 'check CI → review PR → merge → deploy → verify'. "
                    "Once the first step fires, pre-load the likely next 2-3 steps. Present as "
                    "a workflow breadcrumb so the user can jump ahead or skip."
                ),
                "rationale": "Most valuable predictions are mid-workflow, not cold-start.",
            },
            {
                "name": "negative_feedback_loop",
                "detail": (
                    "Track dismissed/ignored predictions as negative signal. If a user ignores "
                    "a prediction type 3x, downweight it by 50%. If they explicitly dismiss it, "
                    "downweight by 80%. Prevents the system from being annoying."
                ),
                "rationale": "Current system has no negative feedback. Only tracks hits.",
            },
            {
                "name": "session_intent_detector",
                "detail": (
                    "Within the first 1-2 messages of a session, classify the user's current mode: "
                    "trading, development, admin, research, social. Load the mode-specific prediction "
                    "model. Adapt language and suggestions to the mode."
                ),
                "rationale": "Generic predictions feel robotic. Mode-aware predictions feel helpful.",
            },
        ],
        "risks": [
            "Progressive disclosure adds UX complexity",
            "Negative feedback loop could suppress valid predictions if user is just busy",
            "Session intent detection from 1-2 messages may misclassify",
        ],
        "dependencies": [
            "UI surface for prediction hints (Telegram inline, TUI sidebar)",
            "memU for feedback storage",
            "Existing orchestrator pipeline for workflow detection",
        ],
        "estimated_effort": "days",
        "confidence": 0.84,
    }


AGENT_PROPOSALS = {
    "lenny": lenny_proposal,
    "rosie": rosie_proposal,
    "macklemore": macklemore_proposal,
    "winnie": winnie_proposal,
}

# ──────────────────────────────────────────────────────────────────────
# Challenge generators — each agent challenges the others
# ──────────────────────────────────────────────────────────────────────

def generate_challenges(
    agent_id: str,
    proposals: list[DeliberationMessage],
) -> list[dict]:
    """Generate challenges from one agent's perspective against all proposals."""

    challenges_by_agent = {
        "lenny": {
            "rosie": {
                "challenge_type": "feasibility",
                "concern": (
                    "Markov chain needs 100+ transitions per state. We have ~40 queries in "
                    "search_history. The model will be wildly unreliable for months. What's "
                    "the minimum viable data threshold before we turn this on?"
                ),
                "evidence": "next_intent.py pulls LIMIT 40 from search_history. That's total, not per-state.",
                "suggested_alternative": (
                    "Start with the Pattern Library (8 patterns, 85%+ accuracy) as Markov priors. "
                    "Only transition to learned Markov when a state has 30+ observations."
                ),
                "severity": "high",
            },
            "macklemore": {
                "challenge_type": "risk",
                "concern": (
                    "Pre-stage cache computing results nobody asks for wastes compute and "
                    "could expose stale data. What's the cache hit rate expectation? If it's "
                    "below 40%, the system is burning cycles for nothing."
                ),
                "evidence": "No historical data on what users actually ask after events. Speculation-driven caching.",
                "suggested_alternative": (
                    "Start with lazy caching: first time user asks after event X, cache the result. "
                    "Second time, serve from cache. Only add proactive pre-staging after proving "
                    "cache hit rate > 50%."
                ),
                "severity": "medium",
            },
            "winnie": {
                "challenge_type": "scope",
                "concern": (
                    "Progressive disclosure across 4 tiers + mode detection + workflow breadcrumbs "
                    "is a LOT of UX surface. Can we ship this incrementally? What's the MVP tier?"
                ),
                "evidence": "We have zero prediction accuracy data. Building 4 tiers of UX for an unproven system is premature.",
                "suggested_alternative": (
                    "Ship Tier 3 only (subtle hints) for the first 2 weeks. Measure dismissal "
                    "rates. Then unlock Tier 2. Tier 1 auto-execute only after 2 weeks of >85% accuracy."
                ),
                "severity": "medium",
            },
        },
        "rosie": {
            "lenny": {
                "challenge_type": "risk",
                "concern": (
                    "Circuit breaker pausing after 3 misses is too aggressive for a learning system. "
                    "Early-stage predictions WILL miss frequently. The breaker could keep the system "
                    "permanently paused during the critical learning period."
                ),
                "evidence": "Current accuracy is 0%. System would trip immediately and never recover.",
                "suggested_alternative": (
                    "Use graduated thresholds: first 30 days allow 50% miss rate before breaker, "
                    "days 30-60 tighten to 30%, steady-state at 3 consecutive misses. "
                    "Include 'learning mode' flag."
                ),
                "severity": "high",
            },
            "macklemore": {
                "challenge_type": "architecture",
                "concern": (
                    "Event taxonomy registry is the right idea but needs to be learned, not hardcoded. "
                    "Manually maintaining event→intent mappings doesn't scale. The mapping should "
                    "evolve from observed correlations."
                ),
                "evidence": "Hardcoded regex patterns in overseer_orchestrator.py already drift from reality.",
                "suggested_alternative": (
                    "Seed with hardcoded mappings, but add a correlation tracker that observes "
                    "actual event→user-action pairs and proposes mapping updates weekly."
                ),
                "severity": "medium",
            },
            "winnie": {
                "challenge_type": "priority",
                "concern": (
                    "Negative feedback loop is critical and should be Phase 1, not an add-on. "
                    "Without it, we can't distinguish 'prediction was wrong' from 'prediction was "
                    "right but user was busy'. This poisons ALL accuracy data."
                ),
                "evidence": "Current system has no negative signal path. All non-responses are treated as misses.",
                "suggested_alternative": (
                    "Make negative feedback the FIRST component built. Without it, accuracy "
                    "tracking (Lenny's component) produces garbage numbers."
                ),
                "severity": "high",
            },
        },
        "macklemore": {
            "lenny": {
                "challenge_type": "architecture",
                "concern": (
                    "Accuracy tracker as a standalone component adds another write path. "
                    "Should be a NATS event that multiple consumers can process — not a "
                    "bespoke database table with its own API."
                ),
                "evidence": "We already have NATS publisher. Prediction outcomes should be events on the mesh.",
                "suggested_alternative": (
                    "Publish prediction outcomes as SwarmEvents (PREDICTION_HIT / PREDICTION_MISS). "
                    "Any consumer can aggregate accuracy. No new database table needed."
                ),
                "severity": "medium",
            },
            "rosie": {
                "challenge_type": "feasibility",
                "concern": (
                    "External context enricher (market hours, git events, calendar) adds 3+ "
                    "new API dependencies. Each is a failure point. We already struggle with "
                    "Brave Search being broken for weeks."
                ),
                "evidence": "Active blockers in MEMORY.md: Brave Search broken since 2026-02-14.",
                "suggested_alternative": (
                    "Start with LOCAL context only: file modification times, git log, cron "
                    "job outcomes. These never fail. Add external APIs one at a time after "
                    "local context proves value."
                ),
                "severity": "high",
            },
            "winnie": {
                "challenge_type": "feasibility",
                "concern": (
                    "Session intent detector from 1-2 messages works for humans but agents "
                    "often get tasks injected by cron/heartbeat with no conversational warmup. "
                    "How does mode detection work for automated sessions?"
                ),
                "evidence": "Most agent sessions start from cron payloads, not human conversation.",
                "suggested_alternative": (
                    "Add source-based mode classification: cron → admin, heartbeat → monitoring, "
                    "human DM → classify from content, group chat → classify from topic."
                ),
                "severity": "medium",
            },
        },
        "winnie": {
            "lenny": {
                "challenge_type": "scope",
                "concern": (
                    "Cold start bootstrapper using Pattern Library priors assumes patterns "
                    "transfer across users. They don't — a biotech founder has different "
                    "patterns than a SRE. The priors might hurt more than help."
                ),
                "evidence": "Pattern Library patterns are from THIS team's usage. Not generalizable.",
                "suggested_alternative": (
                    "For cold start, use the user's own first 10 interactions to build initial "
                    "patterns, not borrowed priors. Offer a quick 'preference setup' flow instead."
                ),
                "severity": "low",
            },
            "rosie": {
                "challenge_type": "scope",
                "concern": (
                    "Behavioral cohort analysis is great for a product with thousands of users. "
                    "We have ONE user (Michael). Cohort analysis on N=1 is just... remembering. "
                    "Rename this to 'session mode memory' and simplify."
                ),
                "evidence": "USER.md lists one human: Michael Fethe.",
                "suggested_alternative": (
                    "Simplify to a session mode detector: classify the current session's purpose "
                    "and load relevant predictions. No need for statistical cohort clustering."
                ),
                "severity": "low",
            },
            "macklemore": {
                "challenge_type": "priority",
                "concern": (
                    "Event replay for training is the most valuable component but listed last. "
                    "Without replay, the event taxonomy stays static forever. This should be "
                    "the FIRST infra piece, not an afterthought."
                ),
                "evidence": "Current system has no feedback loop from events to prediction quality.",
                "suggested_alternative": (
                    "Ship event replay + correlation tracker in Phase 1. Everything else builds on "
                    "the feedback loop."
                ),
                "severity": "high",
            },
        },
    }

    results = []
    for msg in proposals:
        other_agent = msg.agent_id
        if other_agent == agent_id or other_agent not in challenges_by_agent.get(agent_id, {}):
            continue
        challenge = challenges_by_agent[agent_id][other_agent]
        results.append({
            "target_message_id": msg.message_id,
            "target_agent": other_agent,
            **challenge,
        })
    return results


# ──────────────────────────────────────────────────────────────────────
# Refinement generators — agents refine based on challenges received
# ──────────────────────────────────────────────────────────────────────

def generate_refinement(
    agent_id: str,
    original_proposal: dict,
    challenges_received: list[dict],
) -> dict:
    """Refine a proposal based on challenges received."""
    refined = {**original_proposal}
    refined["refinement_notes"] = []
    refined["accepted_challenges"] = []
    refined["rejected_challenges"] = []

    for challenge in challenges_received:
        severity = challenge.get("severity", "medium")
        challenger = challenge.get("target_agent", "unknown")

        # Accept high-severity challenges, negotiate medium, reject low
        if severity in ("high", "blocking"):
            refined["accepted_challenges"].append({
                "from": challenger if "from" not in challenge else challenge["from"],
                "concern": challenge["concern"],
                "resolution": challenge.get("suggested_alternative", "Accepted — will implement"),
            })
            refined["refinement_notes"].append(
                f"ACCEPTED ({severity}): {challenge['concern'][:80]}... → {challenge.get('suggested_alternative', 'TBD')[:80]}"
            )
        elif severity == "medium":
            refined["refinement_notes"].append(
                f"NEGOTIATED ({severity}): {challenge['concern'][:80]}... → partial adoption"
            )
        else:
            refined["rejected_challenges"].append({
                "concern": challenge["concern"],
                "reason": "Low severity, original design handles this adequately",
            })

    return refined


# ──────────────────────────────────────────────────────────────────────
# Vote generators — each agent votes on the merged design
# ──────────────────────────────────────────────────────────────────────

def generate_vote(agent_id: str, merged_proposal: dict) -> dict:
    """Generate a vote from an agent's perspective."""
    votes = {
        "lenny": {
            "choice": "conditional",
            "rationale": (
                "Strong design. My conditions: (1) accuracy tracker must be a NATS event "
                "(accepting Macklemore's challenge), (2) circuit breaker must have graduated "
                "thresholds for learning period (accepting Rosie's challenge), (3) all auto-execute "
                "actions must pass the safe_precompute_gate — no exceptions."
            ),
            "conditions": [
                "Accuracy tracker publishes via NATS events, not bespoke DB table",
                "Circuit breaker uses graduated thresholds: 50% miss tolerance for first 30 days",
                "Auto-execute gated by is_reversible AND confidence >= 0.90 AND no_side_effects",
            ],
            "priority_rank": 1,
        },
        "rosie": {
            "choice": "conditional",
            "rationale": (
                "Solid architecture. My conditions: (1) negative feedback loop is Phase 1 "
                "(not an add-on), (2) start with local context only before adding external "
                "APIs (accepting Macklemore's challenge), (3) Markov model bootstraps from "
                "Pattern Library priors until 30+ observations per state."
            ),
            "conditions": [
                "Negative feedback loop ships in Phase 1 alongside accuracy tracking",
                "External context starts local-only (git log, file mtimes, cron outcomes)",
                "Markov model uses Pattern Library priors until 30+ observations per state",
            ],
            "priority_rank": 2,
        },
        "macklemore": {
            "choice": "approve",
            "rationale": (
                "Event-driven architecture on NATS is the right foundation. Accepting the "
                "correlation tracker addition from Rosie — seed hardcoded then learn. "
                "Event replay moves to Phase 1 (accepting Winnie's challenge). "
                "Lazy caching first, proactive pre-staging after proving hit rate > 50%."
            ),
            "conditions": [],
            "priority_rank": 1,
        },
        "winnie": {
            "choice": "conditional",
            "rationale": (
                "Good technical design. My conditions: (1) ship Tier 3 hints only for first "
                "2 weeks before unlocking higher tiers, (2) session mode detection includes "
                "source-based classification for automated sessions (accepting Macklemore's "
                "challenge), (3) 'cohort analysis' renamed to 'session mode memory' (it's N=1)."
            ),
            "conditions": [
                "Progressive disclosure starts at Tier 3 (hints only) for first 2 weeks",
                "Session mode detection includes source-based classification (cron/heartbeat/human)",
                "Rename 'behavioral cohort analysis' to 'session mode memory'",
            ],
            "priority_rank": 2,
        },
    }
    return votes.get(agent_id, {"choice": "abstain", "rationale": "Unknown agent"})


# ──────────────────────────────────────────────────────────────────────
# Main runner
# ──────────────────────────────────────────────────────────────────────

async def run_deliberation(max_rounds: int = 3, timeout: float = 10.0):
    session_id = f"predict-intent-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    logger.info(f"Starting deliberation session: {session_id}")

    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()

    coord = DeliberationCoordinator(
        nc=nc,
        js=js,
        session_id=session_id,
        agents=AGENTS,
        max_rounds=max_rounds,
        quorum=0.75,
    )

    await coord.setup_stream()

    # ── Round 1: Proposals ──
    logger.info("=== PHASE 1: PROPOSALS ===")
    proposal_msgs = []
    for agent_id in AGENTS:
        proposal = AGENT_PROPOSALS[agent_id]()
        msg = DeliberationMessage(
            session_id=session_id,
            phase=Phase.PROPOSE,
            agent_id=agent_id,
            content=proposal,
            round_num=1,
        )
        await coord.publish(msg)
        proposal_msgs.append(msg)
        logger.info(f"  {agent_id}: {proposal['title']}")

    # ── Round 1: Challenges ──
    logger.info("\n=== PHASE 2: CHALLENGES ===")
    challenge_msgs = []
    for agent_id in AGENTS:
        challenges = generate_challenges(agent_id, proposal_msgs)
        for c in challenges:
            msg = DeliberationMessage(
                session_id=session_id,
                phase=Phase.CHALLENGE,
                agent_id=agent_id,
                content=c,
                round_num=1,
                references=[c["target_message_id"]],
            )
            await coord.publish(msg)
            challenge_msgs.append(msg)
            logger.info(
                f"  {agent_id} → {c['target_agent']}: "
                f"[{c['severity']}] {c['challenge_type']} — {c['concern'][:60]}..."
            )

    # ── Round 1: Refinements ──
    logger.info("\n=== PHASE 3: REFINEMENTS ===")
    for agent_id in AGENTS:
        # Get challenges targeting this agent
        my_challenges = [
            m.content for m in challenge_msgs
            if m.content.get("target_agent") == agent_id
        ]
        original = AGENT_PROPOSALS[agent_id]()
        refined = generate_refinement(agent_id, original, my_challenges)

        msg = DeliberationMessage(
            session_id=session_id,
            phase=Phase.REFINE,
            agent_id=agent_id,
            content=refined,
            round_num=1,
            references=[m.message_id for m in challenge_msgs if m.content.get("target_agent") == agent_id],
        )
        await coord.publish(msg)
        accepted = len(refined.get("accepted_challenges", []))
        rejected = len(refined.get("rejected_challenges", []))
        logger.info(f"  {agent_id}: accepted {accepted}, rejected {rejected} challenges")

    # ── Round 1: Votes ──
    logger.info("\n=== PHASE 4: VOTES ===")
    merged = coord.merge_proposals()
    vote_msgs = []
    for agent_id in AGENTS:
        vote = generate_vote(agent_id, merged)
        msg = DeliberationMessage(
            session_id=session_id,
            phase=Phase.VOTE,
            agent_id=agent_id,
            content=vote,
            round_num=1,
        )
        await coord.publish(msg)
        vote_msgs.append(msg)
        logger.info(f"  {agent_id}: {vote['choice']} — {vote['rationale'][:60]}...")

    # ── Resolution ──
    logger.info("\n=== RESOLUTION ===")
    consensus, tally = coord.check_consensus(vote_msgs)
    resolution = coord.resolve()

    logger.info(f"  Consensus reached: {resolution.consensus_reached}")
    logger.info(f"  Vote tally: {resolution.vote_tally}")
    logger.info(f"  Rounds taken: {resolution.rounds_taken}")
    logger.info(f"  Unresolved challenges: {len(resolution.unresolved_challenges)}")
    logger.info(f"  Action items: {len(resolution.action_items)}")

    # ── Write output ──
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_file = OUTPUT_DIR / f"{session_id}.json"
    output_md = OUTPUT_DIR / f"{session_id}.md"

    # JSON output
    output_data = {
        "session_id": session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agents": AGENTS,
        "rounds": resolution.rounds_taken,
        "consensus": resolution.consensus_reached,
        "vote_tally": resolution.vote_tally,
        "merged_proposal": resolution.merged_proposal,
        "action_items": resolution.action_items,
        "unresolved_challenges": resolution.unresolved_challenges,
        "phases": {
            phase.value: [
                {"agent": m.agent_id, "content": m.content, "round": m.round_num}
                for m in msgs
            ]
            for phase, msgs in coord.messages.items()
            if msgs
        },
    }
    output_file.write_text(json.dumps(output_data, indent=2, default=str))
    logger.info(f"  JSON output: {output_file}")

    # Markdown summary
    md_lines = [
        f"# Deliberation: Predictive Intent System",
        f"**Session:** {session_id}",
        f"**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"**Consensus:** {'✅ YES' if resolution.consensus_reached else '❌ NO'}",
        f"**Vote Tally:** {resolution.vote_tally}",
        f"**Rounds:** {resolution.rounds_taken}",
        "",
        "## Proposals",
    ]
    for msg in proposal_msgs:
        c = msg.content
        md_lines.extend([
            f"### {msg.agent_id.title()}: {c['title']}",
            c['description'],
            "",
            "**Components:**",
            *[f"- **{comp['name']}**: {comp['detail'][:120]}..." for comp in c.get('components', [])],
            "",
            f"**Risks:** {', '.join(c.get('risks', []))}",
            f"**Confidence:** {c.get('confidence', 'N/A')}",
            "",
        ])

    md_lines.extend(["## Challenges", ""])
    for msg in challenge_msgs:
        c = msg.content
        md_lines.append(
            f"- **{msg.agent_id}** → **{c.get('target_agent', '?')}** "
            f"[{c.get('severity', '?')}/{c.get('challenge_type', '?')}]: "
            f"{c.get('concern', '')[:120]}..."
        )

    md_lines.extend(["", "## Votes", ""])
    for msg in vote_msgs:
        c = msg.content
        md_lines.append(f"- **{msg.agent_id}**: {c.get('choice', '?')} — {c.get('rationale', '')[:120]}...")
        for cond in c.get("conditions", []):
            md_lines.append(f"  - Condition: {cond}")

    md_lines.extend(["", "## Action Items (from conditional votes)", ""])
    for item in resolution.action_items:
        md_lines.append(f"- [{item.get('owner', '?')}] {item.get('action', '?')} (deadline: {item.get('deadline', '?')})")

    md_lines.extend([
        "",
        "## Merged Architecture (Consensus Components)",
        "",
    ])
    for comp in resolution.merged_proposal.get("components", []):
        md_lines.append(f"- **{comp.get('name', '?')}** (proposed by: {comp.get('proposed_by', '?')}): {comp.get('detail', '')[:150]}...")

    md_lines.extend([
        "",
        "## Implementation Phases",
        "",
        "### Phase 1 (Week 1-2): Foundation",
        "- Event replay stream + correlation tracker (feedback loop)",
        "- Negative feedback loop (dismiss/ignore tracking)",
        "- Accuracy tracker via NATS events (PREDICTION_HIT/MISS)",
        "- Session mode detector (source-based classification)",
        "- Tier 3 prediction hints only (subtle, non-intrusive)",
        "",
        "### Phase 2 (Week 3-4): Learning",
        "- Temporal pattern miner (time-of-day, day-of-week)",
        "- Markov sequence model (bootstrapped from Pattern Library priors)",
        "- Lazy prediction cache (NATS KV, populate on first hit)",
        "- Graduated circuit breaker (50% miss tolerance → 30% → 3 consecutive)",
        "",
        "### Phase 3 (Week 5-6): Acceleration",
        "- Tier 2 pre-computed suggestions (after proving accuracy)",
        "- Proactive pre-staging (after cache hit rate > 50%)",
        "- Local context enricher (git log, file mtimes, cron outcomes)",
        "- Prompt chain/workflow detector",
        "",
        "### Phase 4 (Week 7+): Auto-pilot",
        "- Tier 1 auto-execute (confidence > 90%, reversible only)",
        "- External context APIs (one at a time, with circuit breakers)",
        "- Weekly automated retraining from event replay",
        "- Cross-agent prediction sharing via NATS mesh",
    ])

    output_md.write_text("\n".join(md_lines))
    logger.info(f"  Markdown output: {output_md}")

    await nc.close()
    return output_data


def main():
    ap = argparse.ArgumentParser(description="Run multi-agent deliberation on NATS")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args()

    result = asyncio.run(run_deliberation(max_rounds=args.rounds, timeout=args.timeout))

    print(f"\n{'='*60}")
    print(f"DELIBERATION COMPLETE")
    print(f"Consensus: {'✅' if result['consensus'] else '❌'}")
    print(f"Votes: {result['vote_tally']}")
    print(f"Action items: {len(result['action_items'])}")
    print(f"Output: self_improvement/deliberations/{result['session_id']}.md")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
