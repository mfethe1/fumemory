"""NATS-backed Multi-Agent Deliberation Protocol.

Implements structured consensus loops:
  propose → challenge → refine → vote → resolve

Each agent publishes proposals, challenges, and votes to NATS subjects.
A coordinator collects responses and drives convergence.

Subjects:
  delib.<session_id>.propose    — initial proposals
  delib.<session_id>.challenge  — challenges/pressure tests
  delib.<session_id>.refine     — refined proposals after challenges
  delib.<session_id>.vote       — votes (approve/reject/abstain + rationale)
  delib.<session_id>.resolve    — final merged outcome

All messages are JSON-serialized DeliberationMessage instances.
"""

from __future__ import annotations

import enum
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


class Phase(str, enum.Enum):
    PROPOSE = "propose"
    CHALLENGE = "challenge"
    REFINE = "refine"
    VOTE = "vote"
    RESOLVE = "resolve"


class VoteChoice(str, enum.Enum):
    APPROVE = "approve"
    REJECT = "reject"
    ABSTAIN = "abstain"
    CONDITIONAL = "conditional"  # approve with conditions


@dataclass
class DeliberationMessage:
    session_id: str
    phase: Phase
    agent_id: str
    content: dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    message_id: str = field(default_factory=lambda: str(uuid4())[:12])
    round_num: int = 1
    references: list[str] = field(default_factory=list)  # message_ids this responds to

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode()

    @classmethod
    def from_json(cls, data: bytes) -> "DeliberationMessage":
        d = json.loads(data.decode())
        d["phase"] = Phase(d["phase"])
        return cls(**d)


@dataclass
class Proposal:
    """A structured proposal from one agent."""
    title: str
    description: str
    components: list[dict[str, str]]  # [{name, detail, rationale}]
    risks: list[str]
    dependencies: list[str]
    estimated_effort: str  # "hours" | "days" | "weeks"
    confidence: float  # 0-1


@dataclass
class Challenge:
    """A structured challenge against a proposal."""
    target_message_id: str
    challenge_type: str  # "feasibility" | "scope" | "risk" | "priority" | "architecture"
    concern: str
    evidence: str
    suggested_alternative: str | None = None
    severity: str = "medium"  # "low" | "medium" | "high" | "blocking"


@dataclass
class Vote:
    """A structured vote on a refined proposal."""
    target_message_id: str
    choice: VoteChoice
    rationale: str
    conditions: list[str] = field(default_factory=list)  # for CONDITIONAL votes
    priority_rank: int = 0  # agent's priority ranking of this item


@dataclass
class Resolution:
    """Final merged outcome after consensus."""
    merged_proposal: dict[str, Any]
    vote_tally: dict[str, int]  # {approve: N, reject: N, ...}
    unresolved_challenges: list[str]
    action_items: list[dict[str, str]]  # [{owner, action, deadline}]
    consensus_reached: bool
    rounds_taken: int


class DeliberationCoordinator:
    """Drives the deliberation loop over NATS.

    Usage:
        coord = DeliberationCoordinator(nc, js, session_id, agents)
        # Collect proposals
        await coord.collect_phase(Phase.PROPOSE, timeout_sec=30)
        # Drive challenges
        await coord.collect_phase(Phase.CHALLENGE, timeout_sec=30)
        # Refine
        await coord.collect_phase(Phase.REFINE, timeout_sec=30)
        # Vote
        await coord.collect_phase(Phase.VOTE, timeout_sec=20)
        # Resolve
        resolution = coord.resolve()
    """

    def __init__(
        self,
        nc,  # NATS connection
        js,  # JetStream context
        session_id: str,
        agents: list[str],
        max_rounds: int = 3,
        quorum: float = 0.75,  # fraction of agents needed for consensus
    ):
        self.nc = nc
        self.js = js
        self.session_id = session_id
        self.agents = agents
        self.max_rounds = max_rounds
        self.quorum = quorum
        self.messages: dict[Phase, list[DeliberationMessage]] = {p: [] for p in Phase}
        self.current_round = 1
        self._stream_name = f"DELIB_{session_id.replace('-', '_')}"

    def _subject(self, phase: Phase) -> str:
        return f"delib.{self.session_id}.{phase.value}"

    async def setup_stream(self) -> None:
        """Create JetStream stream for this deliberation session."""
        try:
            await self.js.find_stream_by_subject(f"delib.{self.session_id}.*")
            logger.info(f"Stream for session {self.session_id} already exists")
        except Exception:
            await self.js.add_stream(
                name=self._stream_name,
                subjects=[f"delib.{self.session_id}.*"],
                max_msgs=500,
                max_age=3600 * 4,  # 4 hours retention
            )
            logger.info(f"Created deliberation stream {self._stream_name}")

    async def publish(self, msg: DeliberationMessage) -> None:
        """Publish a deliberation message to the appropriate subject."""
        subject = self._subject(msg.phase)
        await self.js.publish(subject, msg.to_json())
        self.messages[msg.phase].append(msg)
        logger.info(f"Published {msg.phase.value} from {msg.agent_id} (round {msg.round_num})")

    async def collect_phase(
        self,
        phase: Phase,
        timeout_sec: float = 30.0,
        min_responses: int | None = None,
    ) -> list[DeliberationMessage]:
        """Subscribe and collect messages for a phase until timeout or all agents respond."""
        if min_responses is None:
            min_responses = len(self.agents)

        subject = self._subject(phase)
        collected: list[DeliberationMessage] = []
        seen_agents: set[str] = set()
        deadline = time.time() + timeout_sec

        sub = await self.nc.subscribe(subject)

        try:
            while time.time() < deadline and len(seen_agents) < min_responses:
                remaining = max(0.1, deadline - time.time())
                try:
                    raw_msg = await sub.next_msg(timeout=remaining)
                    msg = DeliberationMessage.from_json(raw_msg.data)
                    if msg.round_num == self.current_round:
                        collected.append(msg)
                        seen_agents.add(msg.agent_id)
                        logger.info(
                            f"Collected {phase.value} from {msg.agent_id} "
                            f"({len(seen_agents)}/{min_responses})"
                        )
                except Exception:
                    break
        finally:
            await sub.unsubscribe()

        self.messages[phase].extend(collected)
        return collected

    def check_consensus(self, votes: list[DeliberationMessage]) -> tuple[bool, dict[str, int]]:
        """Check if votes meet quorum threshold."""
        tally: dict[str, int] = {"approve": 0, "reject": 0, "abstain": 0, "conditional": 0}
        for v in votes:
            choice = v.content.get("choice", "abstain")
            tally[choice] = tally.get(choice, 0) + 1

        # Conditional counts as approve for quorum purposes
        approvals = tally["approve"] + tally["conditional"]
        total_cast = sum(tally.values())
        threshold = int(len(self.agents) * self.quorum)

        consensus = approvals >= threshold and tally["reject"] == 0
        return consensus, tally

    def merge_proposals(self) -> dict[str, Any]:
        """Merge all refined proposals into a unified design."""
        proposals = self.messages.get(Phase.REFINE, []) or self.messages.get(Phase.PROPOSE, [])
        if not proposals:
            return {"error": "no proposals collected"}

        # Collect all components, deduplicate by name
        all_components: dict[str, dict] = {}
        all_risks: list[str] = []
        all_deps: list[str] = []

        for msg in proposals:
            content = msg.content
            for comp in content.get("components", []):
                name = comp.get("name", "unnamed")
                if name not in all_components:
                    all_components[name] = {**comp, "proposed_by": msg.agent_id}
                else:
                    # Merge: keep the more detailed version
                    existing = all_components[name]
                    if len(comp.get("detail", "")) > len(existing.get("detail", "")):
                        all_components[name] = {**comp, "proposed_by": msg.agent_id}

            all_risks.extend(content.get("risks", []))
            all_deps.extend(content.get("dependencies", []))

        return {
            "title": "Merged Predictive Intent System",
            "components": list(all_components.values()),
            "risks": list(set(all_risks)),
            "dependencies": list(set(all_deps)),
            "contributing_agents": list({m.agent_id for m in proposals}),
            "round": self.current_round,
        }

    def resolve(self) -> Resolution:
        """Produce the final resolution from all collected phases."""
        votes = self.messages.get(Phase.VOTE, [])
        consensus, tally = self.check_consensus(votes)
        merged = self.merge_proposals()

        # Extract unresolved challenges
        challenges = self.messages.get(Phase.CHALLENGE, [])
        refines = self.messages.get(Phase.REFINE, [])
        challenge_ids = {c.message_id for c in challenges}
        addressed_ids = set()
        for r in refines:
            addressed_ids.update(r.references)
        unresolved = [
            c.content.get("concern", "")
            for c in challenges
            if c.message_id not in addressed_ids
        ]

        # Build action items from conditional votes
        action_items = []
        for v in votes:
            if v.content.get("choice") == "conditional":
                for condition in v.content.get("conditions", []):
                    action_items.append({
                        "owner": v.agent_id,
                        "action": condition,
                        "deadline": "next_sprint",
                    })

        return Resolution(
            merged_proposal=merged,
            vote_tally=tally,
            unresolved_challenges=unresolved,
            action_items=action_items,
            consensus_reached=consensus,
            rounds_taken=self.current_round,
        )

    async def run_full_loop(
        self,
        initial_proposals: list[DeliberationMessage],
        timeout_per_phase: float = 30.0,
    ) -> Resolution:
        """Run the complete deliberation loop until consensus or max rounds."""
        await self.setup_stream()

        for round_num in range(1, self.max_rounds + 1):
            self.current_round = round_num
            logger.info(f"=== Deliberation Round {round_num}/{self.max_rounds} ===")

            # Phase 1: Proposals (round 1 uses initial, later rounds use refined)
            if round_num == 1:
                for p in initial_proposals:
                    p.round_num = round_num
                    await self.publish(p)
            # (refined proposals from previous round carry forward)

            # Phase 2: Challenges
            logger.info("Collecting challenges...")
            challenges = await self.collect_phase(
                Phase.CHALLENGE, timeout_sec=timeout_per_phase
            )
            if not challenges:
                logger.info("No challenges — moving to vote")

            # Phase 3: Refine (only if challenges exist)
            if challenges:
                logger.info("Collecting refinements...")
                await self.collect_phase(
                    Phase.REFINE, timeout_sec=timeout_per_phase
                )

            # Phase 4: Vote
            logger.info("Collecting votes...")
            votes = await self.collect_phase(
                Phase.VOTE, timeout_sec=timeout_per_phase
            )

            # Check consensus
            consensus, tally = self.check_consensus(votes)
            logger.info(f"Round {round_num} tally: {tally}, consensus: {consensus}")

            if consensus:
                break
            elif round_num < self.max_rounds:
                logger.info("No consensus — entering next refinement round")

        return self.resolve()
