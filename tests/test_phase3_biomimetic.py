"""Tests for Phase 3: Biomimetic Mechanics — salience, decay, procedural memory, dream consolidation."""

from __future__ import annotations

import math
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

from memu.decay import compute_final_score, compute_effective_decay_rate, should_deduplicate
from memu.models import MemoryType, MemoryCreate, Memory


# ---------------------------------------------------------------------------
# Salience-weighted decay tests
# ---------------------------------------------------------------------------


class TestComputeEffectiveDecayRate:
    def test_zero_salience_gives_full_decay(self):
        rate = compute_effective_decay_rate(0.01, 0.0)
        assert rate == 0.01

    def test_half_salience_gives_half_decay(self):
        rate = compute_effective_decay_rate(0.01, 0.5)
        assert abs(rate - 0.005) < 1e-9

    def test_full_salience_gives_zero_decay(self):
        rate = compute_effective_decay_rate(0.01, 1.0)
        assert rate == 0.0

    def test_high_salience_near_zero_decay(self):
        rate = compute_effective_decay_rate(0.01, 0.99)
        assert rate == pytest.approx(0.0001, abs=1e-9)

    def test_clamps_salience_above_one(self):
        rate = compute_effective_decay_rate(0.01, 1.5)
        assert rate == 0.0

    def test_clamps_salience_below_zero(self):
        rate = compute_effective_decay_rate(0.01, -0.5)
        assert rate == 0.01


class TestComputeFinalScoreWithSalience:
    def test_high_salience_memory_decays_slower(self):
        """A Sev-1 outage (salience=0.99) should score higher than routine (salience=0.1) after 30 days."""
        old_time = datetime.now(timezone.utc) - timedelta(days=30)

        score_critical = compute_final_score(
            similarity=0.8, created_at=old_time, access_count=1,
            salience_score=0.99,
        )
        score_routine = compute_final_score(
            similarity=0.8, created_at=old_time, access_count=1,
            salience_score=0.1,
        )
        assert score_critical > score_routine, (
            f"Critical memory ({score_critical:.4f}) should score higher than "
            f"routine ({score_routine:.4f}) after 30 days"
        )

    def test_permanent_memory_never_decays(self):
        """Salience=1.0 should produce zero effective decay."""
        very_old = datetime.now(timezone.utc) - timedelta(days=365)
        score = compute_final_score(
            similarity=0.8, created_at=very_old, access_count=1,
            salience_score=1.0,
        )
        # With zero decay, the score should be close to the fresh score
        fresh_score = compute_final_score(
            similarity=0.8, created_at=datetime.now(timezone.utc), access_count=1,
            salience_score=1.0,
        )
        assert abs(score - fresh_score) < 0.01, (
            f"Permanent memory score ({score:.4f}) should be close to fresh ({fresh_score:.4f})"
        )

    def test_default_salience_backward_compatible(self):
        """Default salience=0.5 should produce reasonable scores."""
        now = datetime.now(timezone.utc)
        score = compute_final_score(
            similarity=0.8, created_at=now, access_count=1,
        )
        assert score > 0

    def test_backward_compatible_signature(self):
        """Old callers without salience_score should still work."""
        now = datetime.now(timezone.utc)
        score = compute_final_score(
            similarity=0.8, created_at=now, access_count=1,
            decay_rate=0.01, temporal_weight=0.3, min_score=0.1,
        )
        assert score > 0


# ---------------------------------------------------------------------------
# Procedural memory type tests
# ---------------------------------------------------------------------------


class TestProceduralMemoryType:
    def test_procedural_type_exists(self):
        assert MemoryType.procedural == "procedural"

    def test_procedural_in_enum_values(self):
        assert "procedural" in [mt.value for mt in MemoryType]

    def test_memory_create_with_procedural(self):
        mc = MemoryCreate(
            content="Debug OOM: 1) check heap 2) analyze GC 3) fix leak",
            memory_type=MemoryType.procedural,
            agent_id="agent-lenny",
            salience_score=0.85,
        )
        assert mc.memory_type == MemoryType.procedural
        assert mc.salience_score == 0.85


# ---------------------------------------------------------------------------
# Memory model with new fields
# ---------------------------------------------------------------------------


class TestMemoryModelNewFields:
    def test_salience_score_field(self):
        mc = MemoryCreate(content="test", agent_id="a", salience_score=0.9)
        assert mc.salience_score == 0.9

    def test_salience_score_default(self):
        mc = MemoryCreate(content="test", agent_id="a")
        assert mc.salience_score == 0.5

    def test_salience_score_bounds(self):
        with pytest.raises(Exception):
            MemoryCreate(content="test", agent_id="a", salience_score=1.5)
        with pytest.raises(Exception):
            MemoryCreate(content="test", agent_id="a", salience_score=-0.1)

    def test_memory_has_searchable_field(self):
        from uuid import uuid4
        m = Memory(
            id=uuid4(), content="test", memory_type="fact", agent_id="a",
            metadata={}, parent_id=None, confidence=1.0, access_count=0,
            salience_score=0.5, searchable=True,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        assert m.searchable is True
        assert m.salience_score == 0.5


# ---------------------------------------------------------------------------
# Dream Consolidation Workflow tests
# ---------------------------------------------------------------------------


class TestDreamConsolidationWorkflow:
    def test_workflow_class_exists(self):
        from memu.temporal_worker.workflows import DreamConsolidationWorkflow
        assert DreamConsolidationWorkflow is not None

    def test_prospective_memory_workflow_exists(self):
        from memu.temporal_worker.workflows import ProspectiveMemoryWorkflow
        assert ProspectiveMemoryWorkflow is not None

