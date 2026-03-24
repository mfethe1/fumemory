"""Tests for memu.memory_agent — triplet extraction, entity resolution, predicate constraints."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from memu.memory_agent import (
    RelationshipType,
    Triplet,
    TripletExtractionResult,
    EntityResolutionResult,
    extract_triplets,
    find_similar_entity,
    verify_entity_match,
    resolve_and_get_node_id,
    ENTITY_SIMILARITY_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Predicate Enum tests
# ---------------------------------------------------------------------------


class TestRelationshipType:
    def test_all_predicates_are_uppercase(self):
        for rt in RelationshipType:
            assert rt.value == rt.value.upper(), f"{rt.value} is not uppercase"

    def test_critical_predicates_exist(self):
        assert RelationshipType.RELATES_TO
        assert RelationshipType.DEPENDS_ON
        assert RelationshipType.CAUSES
        assert RelationshipType.SUPERSEDES
        assert RelationshipType.CONTRADICTS
        assert RelationshipType.DERIVED_FROM

    def test_invalid_predicate_rejected(self):
        with pytest.raises(ValueError):
            RelationshipType("RANDOM_GARBAGE")


# ---------------------------------------------------------------------------
# Triplet model tests
# ---------------------------------------------------------------------------


class TestTripletModel:
    def test_valid_triplet(self):
        t = Triplet(
            subject="PostgreSQL",
            predicate=RelationshipType.USES,
            object="pgvector",
            confidence=0.9,
        )
        assert t.subject == "PostgreSQL"
        assert t.predicate == RelationshipType.USES
        assert t.object == "pgvector"

    def test_empty_subject_rejected(self):
        with pytest.raises(Exception):
            Triplet(subject="", predicate=RelationshipType.USES, object="pgvector")

    def test_confidence_bounds(self):
        with pytest.raises(Exception):
            Triplet(subject="A", predicate=RelationshipType.USES, object="B", confidence=1.5)

    def test_extraction_result_model(self):
        result = TripletExtractionResult(triplets=[
            Triplet(subject="A", predicate=RelationshipType.CAUSES, object="B"),
        ])
        assert len(result.triplets) == 1


# ---------------------------------------------------------------------------
# Extract triplets (mocked LLM)
# ---------------------------------------------------------------------------


class TestExtractTriplets:
    @pytest.mark.asyncio
    async def test_returns_empty_without_api_key(self):
        with patch("memu.memory_agent.LLM_API_KEY", ""):
            result = await extract_triplets("PostgreSQL uses pgvector for embeddings")
            assert result == []

    @pytest.mark.asyncio
    async def test_parses_valid_llm_response(self):
        mock_response = {
            "triplets": [
                {"subject": "PostgreSQL", "predicate": "USES", "object": "pgvector", "confidence": 0.9},
                {"subject": "memU", "predicate": "DEPENDS_ON", "object": "NATS", "confidence": 0.85},
            ]
        }
        with patch("memu.memory_agent._call_llm_json", new_callable=AsyncMock, return_value=mock_response):
            result = await extract_triplets("PostgreSQL uses pgvector. memU depends on NATS.")
            assert len(result) == 2
            assert result[0].predicate == RelationshipType.USES
            assert result[1].predicate == RelationshipType.DEPENDS_ON

    @pytest.mark.asyncio
    async def test_handles_invalid_predicate_gracefully(self):
        mock_response = {
            "triplets": [
                {"subject": "A", "predicate": "INVALID_PRED", "object": "B"},
            ]
        }
        with patch("memu.memory_agent._call_llm_json", new_callable=AsyncMock, return_value=mock_response):
            result = await extract_triplets("some text")
            # Should return empty — Pydantic rejects invalid predicate
            assert result == []

    @pytest.mark.asyncio
    async def test_handles_llm_failure(self):
        with patch("memu.memory_agent._call_llm_json", new_callable=AsyncMock, return_value=None):
            result = await extract_triplets("some text")
            assert result == []


# ---------------------------------------------------------------------------
# Entity Resolution tests
# ---------------------------------------------------------------------------


class TestFindSimilarEntity:
    @pytest.mark.asyncio
    async def test_returns_none_without_pool(self):
        result = await find_similar_entity("test", AsyncMock(return_value=[0.1]*384), None)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_below_threshold(self):
        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[
            {"id": "abc", "content": "different thing", "similarity": 0.5}
        ])
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await find_similar_entity(
            "PostgreSQL", AsyncMock(return_value=[0.1]*384), mock_pool
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_match_above_threshold(self):
        mock_pool = MagicMock()
        mock_conn = AsyncMock()


class TestVerifyEntityMatch:
    @pytest.mark.asyncio
    async def test_fails_closed_without_api_key(self):
        """If LLM is unavailable, NEVER merge — fail closed."""
        with patch("memu.memory_agent.LLM_API_KEY", ""):
            result = await verify_entity_match("Postgres", "PostgreSQL")
            assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_for_same_entity(self):
        mock_response = {"is_same_entity": True, "reasoning": "Same database"}
        with patch("memu.memory_agent._call_llm_json", new_callable=AsyncMock, return_value=mock_response):
            result = await verify_entity_match("Postgres", "PostgreSQL")
            assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_for_different_entities(self):
        mock_response = {"is_same_entity": False, "reasoning": "Different systems"}
        with patch("memu.memory_agent._call_llm_json", new_callable=AsyncMock, return_value=mock_response):
            result = await verify_entity_match("PostgreSQL", "MySQL")
            assert result is False


class TestResolveAndGetNodeId:
    @pytest.mark.asyncio
    async def test_returns_original_when_no_candidate(self):
        with patch("memu.memory_agent.find_similar_entity", new_callable=AsyncMock, return_value=None):
            name, merged = await resolve_and_get_node_id("NewEntity", "context", None, None)
            assert name == "NewEntity"
            assert merged is False

    @pytest.mark.asyncio
    async def test_merges_when_llm_confirms(self):
        candidate = {"id": "abc", "content": "PostgreSQL", "similarity": 0.95}
        with patch("memu.memory_agent.find_similar_entity", new_callable=AsyncMock, return_value=candidate):
            with patch("memu.memory_agent.verify_entity_match", new_callable=AsyncMock, return_value=True):
                name, merged = await resolve_and_get_node_id("Postgres", "context", None, None)
                assert name == "PostgreSQL"
                assert merged is True

    @pytest.mark.asyncio
    async def test_does_not_merge_when_llm_rejects(self):
        """Critical invariant: vector similarity alone is NOT enough to merge."""
        candidate = {"id": "abc", "content": "PostgreSQL", "similarity": 0.95}
        with patch("memu.memory_agent.find_similar_entity", new_callable=AsyncMock, return_value=candidate):
            with patch("memu.memory_agent.verify_entity_match", new_callable=AsyncMock, return_value=False):
                name, merged = await resolve_and_get_node_id("Postgres", "context", None, None)
                assert name == "Postgres"
                assert merged is False

