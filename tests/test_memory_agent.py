"""Tests for the memU memory agent — compaction, concept indexing, and RRF fusion."""

import math
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from memu.concept_search import rrf_fuse
from memu.memory_agent import (
    cluster_by_similarity,
    cosine_similarity,
    extract_concepts,
    generate_summary,
)


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------

class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0, 1.0]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_empty_vectors(self):
        assert cosine_similarity([], []) == 0.0

    def test_mismatched_lengths(self):
        assert cosine_similarity([1.0, 2.0], [1.0]) == 0.0

    def test_zero_vector(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


# ---------------------------------------------------------------------------
# RRF Fusion
# ---------------------------------------------------------------------------

class TestRRFFusion:
    def test_single_result_set_passthrough(self):
        results = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        fused = rrf_fuse([results])
        ids = [r["id"] for r in fused]
        assert ids == ["a", "b", "c"]

    def test_merges_duplicate_ids(self):
        set1 = [{"id": "a"}, {"id": "b"}]
        set2 = [{"id": "b"}, {"id": "c"}]
        fused = rrf_fuse([set1, set2])
        ids = [r["id"] for r in fused]
        # "b" appears in both, gets boosted
        assert ids[0] == "b"
        assert set(ids) == {"a", "b", "c"}

    def test_three_way_fusion(self):
        set1 = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        set2 = [{"id": "c"}, {"id": "a"}, {"id": "d"}]
        set3 = [{"id": "a"}, {"id": "e"}]
        fused = rrf_fuse([set1, set2, set3])
        # "a" is rank 1 in all three → highest RRF
        assert fused[0]["id"] == "a"

    def test_rrf_score_attached(self):
        results = [{"id": "x"}]
        fused = rrf_fuse([results])
        assert "rrf_score" in fused[0]
        assert fused[0]["rrf_score"] > 0

    def test_empty_result_sets(self):
        fused = rrf_fuse([[], []])
        assert fused == []

    def test_k_parameter_affects_score(self):
        # Separate dict instances to avoid mutation cross-contamination
        fused_k60 = rrf_fuse([[{"id": "a"}, {"id": "b"}]], k=60)
        fused_k1 = rrf_fuse([[{"id": "a"}, {"id": "b"}]], k=1)
        # Top item's score: 1/(k+1). Lower k → higher score
        assert fused_k1[0]["rrf_score"] > fused_k60[0]["rrf_score"]


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def make_vector(dim=4, value=1.0):
    """Make a unit vector in dimension 0."""
    v = [0.0] * dim
    v[0] = value
    mag = math.sqrt(sum(x * x for x in v))
    return [x / mag for x in v]


def make_orthogonal_vector(dim=4):
    """Make a unit vector in dimension 1."""
    v = [0.0] * dim
    v[1] = 1.0
    return v


class TestClustering:
    def _make_memory(self, embedding, decay_score=1.0, mid=None):
        return {
            "id": mid or str(uuid.uuid4()),
            "content": "test memory",
            "embedding": embedding,
            "agent_id": "test",
            "memory_type": "fact",
            "decay_score": decay_score,
            "created_at": None,
        }

    def test_clusters_similar_memories(self):
        # Use EMBEDDING_DIMS-length vectors to pass the dim check
        import memu.memory_agent as ma
        dim = ma.EMBEDDING_DIMS
        v1 = [1.0] + [0.0] * (dim - 1)
        v2_raw = [0.99, 0.1] + [0.0] * (dim - 2)
        mag2 = math.sqrt(sum(x * x for x in v2_raw))
        v2_norm = [x / mag2 for x in v2_raw]
        v3_raw = [0.98, 0.05] + [0.0] * (dim - 2)
        mag3 = math.sqrt(sum(x * x for x in v3_raw))
        v3_norm = [x / mag3 for x in v3_raw]

        mems = [
            self._make_memory(v1),
            self._make_memory(v2_norm),
            self._make_memory(v3_norm),
        ]
        clusters = cluster_by_similarity(mems, threshold=0.95, min_size=3, max_size=25)
        assert len(clusters) == 1
        assert len(clusters[0]) == 3

    def test_does_not_cluster_orthogonal(self):
        v1 = [1.0, 0.0, 0.0, 0.0]
        v2 = [0.0, 1.0, 0.0, 0.0]
        v3 = [0.0, 0.0, 1.0, 0.0]
        mems = [self._make_memory(v1), self._make_memory(v2), self._make_memory(v3)]
        clusters = cluster_by_similarity(mems, threshold=0.95, min_size=3)
        assert len(clusters) == 0

    def test_no_memory_in_two_clusters(self):
        v_base = [1.0, 0.0, 0.0, 0.0]
        mems = [self._make_memory(v_base) for _ in range(6)]
        clusters = cluster_by_similarity(mems, threshold=0.99, min_size=3, max_size=10)
        all_ids = [str(m["id"]) for c in clusters for m in c]
        assert len(all_ids) == len(set(all_ids)), "Duplicate memory in multiple clusters"

    def test_skips_memories_without_embeddings(self):
        mem_with = {"id": str(uuid.uuid4()), "content": "x", "embedding": [1.0, 0.0], "agent_id": None, "decay_score": 1.0, "created_at": None}
        mem_without = {"id": str(uuid.uuid4()), "content": "y", "embedding": None, "agent_id": None, "decay_score": 1.0, "created_at": None}
        # Should not crash
        clusters = cluster_by_similarity([mem_with, mem_without], threshold=0.9, min_size=2)
        assert isinstance(clusters, list)


# ---------------------------------------------------------------------------
# Concept extraction (with mocked Ollama)
# ---------------------------------------------------------------------------

class TestConceptExtraction:
    @pytest.mark.asyncio
    async def test_parses_comma_separated_output(self):
        with patch("memu.memory_agent.ollama_generate", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = "deployment, kubernetes, CI/CD, postgres, migration"
            concepts = await extract_concepts("some content about deployments", "test-model")
            assert "deployment" in concepts
            assert "kubernetes" in concepts
            assert len(concepts) <= 10

    @pytest.mark.asyncio
    async def test_falls_back_on_empty_response(self):
        with patch("memu.memory_agent.ollama_generate", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = ""
            concepts = await extract_concepts("The Deployment failed because migrations were skipped.", "test-model")
            # Should return fallback extracted words
            assert isinstance(concepts, list)

    @pytest.mark.asyncio
    async def test_limits_to_10_concepts(self):
        with patch("memu.memory_agent.ollama_generate", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = ", ".join(f"concept{i}" for i in range(20))
            concepts = await extract_concepts("x", "test-model")
            assert len(concepts) <= 10


# ---------------------------------------------------------------------------
# Summary generation (with mocked Ollama)
# ---------------------------------------------------------------------------

class TestSummaryGeneration:
    @pytest.mark.asyncio
    async def test_generates_summary(self):
        with patch("memu.memory_agent.ollama_generate", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = "Always run migrations before deployments to avoid failures."
            memories = ["Deployment failed: forgot migrations", "Migrations skipped caused data loss"]
            summary = await generate_summary(memories, "test-model", "lenny")
            assert "migration" in summary.lower()

    @pytest.mark.asyncio
    async def test_fallback_on_empty_llm_response(self):
        with patch("memu.memory_agent.ollama_generate", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = ""
            memories = ["Memory about something important"]
            summary = await generate_summary(memories, "test-model")
            assert len(summary) > 0  # Should have fallback


# ---------------------------------------------------------------------------
# Migration idempotency
# ---------------------------------------------------------------------------

class TestMigrationIdempotency:
    def test_migration_file_exists(self):
        import pathlib
        path = pathlib.Path(__file__).parent.parent / "memu" / "migrations" / "014_memory_agent.sql"
        assert path.exists()

    def test_uses_if_not_exists_for_tables(self):
        import pathlib
        import re
        sql = (pathlib.Path(__file__).parent.parent / "memu" / "migrations" / "014_memory_agent.sql").read_text()
        # Every CREATE TABLE should have IF NOT EXISTS
        bare_creates = re.findall(r"CREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS)", sql, re.IGNORECASE)
        assert len(bare_creates) == 0, f"Non-idempotent CREATE TABLE: {bare_creates}"

    def test_uses_if_not_exists_for_indexes(self):
        import pathlib
        import re
        sql = (pathlib.Path(__file__).parent.parent / "memu" / "migrations" / "014_memory_agent.sql").read_text()
        # CREATE INDEX (outside DO blocks) should have IF NOT EXISTS
        # Only check top-level statements, not those inside DO $$ blocks
        bare_indexes = re.findall(r"^CREATE\s+INDEX\s+(?!IF\s+NOT\s+EXISTS)", sql, re.IGNORECASE | re.MULTILINE)
        assert len(bare_indexes) == 0, f"Non-idempotent CREATE INDEX: {bare_indexes}"

    def test_creates_required_tables(self):
        import pathlib
        sql = (pathlib.Path(__file__).parent.parent / "memu" / "migrations" / "014_memory_agent.sql").read_text()
        assert "memory_compactions" in sql
        assert "memory_concept_index" in sql
        assert "memory_agent_runs" in sql

    def test_creates_fts_infrastructure(self):
        import pathlib
        sql = (pathlib.Path(__file__).parent.parent / "memu" / "migrations" / "014_memory_agent.sql").read_text()
        assert "fts_vector" in sql
        assert "pg_trgm" in sql
        assert "tsvector" in sql
