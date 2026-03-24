"""Semantic Ingestion Pipeline — LLM-based triplet extraction + entity resolution.

Extracts [Subject, Predicate, Object] triplets from ingested text using
structured LLM outputs constrained to a strict Pydantic Enum of allowed
relationship types (prevents graph fragmentation).

Entity resolution uses a two-stage pipeline:
  1. Vector similarity > 0.90 for candidate matching
  2. LLM contextual verification ("Are these the exact same entity?")
  3. Only then: Cypher MERGE (never merge on cosine similarity alone)
"""

from __future__ import annotations

import json
import logging
import os
from enum import Enum
from typing import Any

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Predicate Enum — strict constraint to prevent graph fragmentation
# ---------------------------------------------------------------------------


class RelationshipType(str, Enum):
    """Allowed relationship types for the knowledge graph.

    ALL triplet predicates MUST be one of these values.
    The LLM is constrained via structured output to only produce these.
    """
    RELATES_TO = "RELATES_TO"
    DEPENDS_ON = "DEPENDS_ON"
    CAUSES = "CAUSES"
    CAUSED_BY = "CAUSED_BY"
    PART_OF = "PART_OF"
    CONTAINS = "CONTAINS"
    USES = "USES"
    USED_BY = "USED_BY"
    CREATED_BY = "CREATED_BY"
    CREATES = "CREATES"
    MODIFIES = "MODIFIES"
    MODIFIED_BY = "MODIFIED_BY"
    TRIGGERS = "TRIGGERS"
    TRIGGERED_BY = "TRIGGERED_BY"
    SUPERSEDES = "SUPERSEDES"
    CONTRADICTS = "CONTRADICTS"
    DERIVED_FROM = "DERIVED_FROM"
    SIMILAR_TO = "SIMILAR_TO"
    INSTANCE_OF = "INSTANCE_OF"
    OWNS = "OWNS"
    OWNED_BY = "OWNED_BY"
    COMMUNICATES_WITH = "COMMUNICATES_WITH"
    LOCATED_IN = "LOCATED_IN"
    OCCURRED_AT = "OCCURRED_AT"
    PRECEDED_BY = "PRECEDED_BY"
    FOLLOWED_BY = "FOLLOWED_BY"


# ---------------------------------------------------------------------------
# Pydantic models for structured LLM output
# ---------------------------------------------------------------------------


class Triplet(BaseModel):
    """A single [Subject, Predicate, Object] triplet."""
    subject: str = Field(..., min_length=1, max_length=256, description="Entity name")
    predicate: RelationshipType = Field(..., description="Relationship type from allowed enum")
    object: str = Field(..., min_length=1, max_length=256, description="Entity name")
    confidence: float = Field(0.8, ge=0.0, le=1.0, description="Extraction confidence")


class TripletExtractionResult(BaseModel):
    """Structured output from the triplet extraction LLM call."""
    triplets: list[Triplet] = Field(default_factory=list)


class EntityResolutionResult(BaseModel):
    """LLM verification: are two entities the same?"""
    is_same_entity: bool = Field(..., description="True if entities refer to the same thing")
    reasoning: str = Field("", description="Brief explanation")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LLM_BASE_URL = os.environ.get("TRIPLET_LLM_URL", os.environ.get("PROFILER_LLM_URL", "https://api.openai.com/v1"))
LLM_MODEL = os.environ.get("TRIPLET_LLM_MODEL", os.environ.get("PROFILER_LLM_MODEL", "gpt-4o-mini"))
LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "")

ENTITY_SIMILARITY_THRESHOLD = 0.90
MAX_TRIPLETS_PER_TEXT = 10

EXTRACTION_SYSTEM_PROMPT = """\
You are a knowledge graph extraction engine. Given text, extract factual
relationships as [Subject, Predicate, Object] triplets.

RULES:
1. Subject and Object must be specific named entities, concepts, or systems.
2. Predicate MUST be one of these exact values: {predicates}
3. Extract up to {max_triplets} triplets. Only extract clear, factual relationships.
4. If no clear relationships exist, return an empty triplets list.

Output ONLY valid JSON matching this schema:
{schema}
"""

ENTITY_RESOLUTION_PROMPT = """\
Are these two entities referring to the exact same thing?

Entity A: "{entity_a}"
Entity B: "{entity_b}"
Context A: "{context_a}"
Context B: "{context_b}"

Answer with JSON: {{"is_same_entity": true/false, "reasoning": "brief explanation"}}
"""




# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------


async def _call_llm_json(system: str, user: str, max_tokens: int = 1000) -> dict | None:
    """Call LLM with JSON response format. Returns parsed dict or None."""
    if not LLM_API_KEY:
        logger.debug("No LLM API key — skipping LLM call")
        return None

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}",
    }
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return json.loads(content)
    except Exception as exc:
        logger.warning("LLM call failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Triplet Extraction
# ---------------------------------------------------------------------------


async def extract_triplets(text: str) -> list[Triplet]:
    """Extract [Subject, Predicate, Object] triplets from text using LLM.

    Predicates are constrained to the RelationshipType enum.
    Returns an empty list if extraction fails or no triplets found.
    """
    predicates = ", ".join(r.value for r in RelationshipType)
    schema = TripletExtractionResult.model_json_schema()

    system = EXTRACTION_SYSTEM_PROMPT.format(
        predicates=predicates,
        max_triplets=MAX_TRIPLETS_PER_TEXT,
        schema=json.dumps(schema, indent=2),
    )

    result = await _call_llm_json(system, f"Extract triplets from:\n\n{text[:4000]}")
    if result is None:
        return []

    try:
        parsed = TripletExtractionResult.model_validate(result)
        return parsed.triplets[:MAX_TRIPLETS_PER_TEXT]
    except Exception as exc:
        logger.warning("Triplet parsing failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Entity Resolution (Two-Stage: Vector + LLM)
# ---------------------------------------------------------------------------


async def find_similar_entity(
    entity_name: str,
    get_embedding: Any,
    pool: Any,
    threshold: float = ENTITY_SIMILARITY_THRESHOLD,
) -> dict | None:
    """Stage 1: Find candidate matching entity via vector similarity.

    Returns the best match above threshold, or None.
    """
    embedding = None
    if get_embedding is not None:
        try:
            embedding = await get_embedding(entity_name)
        except Exception:
            return None

    if embedding is None or pool is None:
        return None

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content, 1 - (embedding <=> $1::vector) AS similarity
            FROM memories
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT 1
            """,
            str(embedding),
        )

    if not rows:
        return None

    row = rows[0]
    if row["similarity"] >= threshold:
        return {
            "id": str(row["id"]),
            "content": row["content"],
            "similarity": float(row["similarity"]),
        }
    return None


async def verify_entity_match(
    entity_a: str,
    entity_b: str,
    context_a: str = "",
    context_b: str = "",
) -> bool:
    """Stage 2: LLM contextual verification — are these the same entity?

    NEVER merge entities based on vector similarity alone.
    This LLM check is MANDATORY before any Cypher MERGE.
    """
    prompt = ENTITY_RESOLUTION_PROMPT.format(
        entity_a=entity_a,
        entity_b=entity_b,
        context_a=context_a[:500],
        context_b=context_b[:500],
    )

    result = await _call_llm_json(
        "You are an entity resolution judge. Determine if two entities are the same.",
        prompt,
        max_tokens=200,
    )

    if result is None:
        return False  # Fail closed — don't merge if LLM unavailable

    try:
        parsed = EntityResolutionResult.model_validate(result)
        return parsed.is_same_entity
    except Exception:
        return False


async def resolve_and_get_node_id(
    entity_name: str,
    context: str,
    get_embedding: Any,
    pool: Any,
) -> tuple[str, bool]:
    """Two-stage entity resolution: vector candidates → LLM verification.

    Returns:
        (entity_name_to_use, was_merged): If merged, returns the existing
        entity name. If new, returns the original name.
    """
    candidate = await find_similar_entity(entity_name, get_embedding, pool)
    if candidate is None:
        return entity_name, False

    # Stage 2: LLM verification
    is_same = await verify_entity_match(
        entity_a=entity_name,
        entity_b=candidate["content"],
        context_a=context,
        context_b=candidate["content"],
    )

    if is_same:
        logger.info(
            "Entity resolved: '%s' → '%s' (sim=%.3f)",
            entity_name, candidate["content"], candidate["similarity"],
        )
        return candidate["content"], True

    return entity_name, False


# ---------------------------------------------------------------------------
# Graph Writing (Apache AGE)
# ---------------------------------------------------------------------------


async def write_triplets_to_graph(
    triplets: list[Triplet],
    pool: Any,
    get_embedding: Any = None,
    graph_name: str = "memu_graph",
    source_context: str = "",
) -> int:
    """Write extracted triplets to Apache AGE with entity resolution.

    For each triplet:
      1. Resolve subject and object via two-stage entity resolution
      2. MERGE nodes (only if LLM-verified)
      3. CREATE relationship edge with the constrained predicate

    Returns the number of edges successfully created.
    """
    if not triplets or pool is None:
        return 0

    created = 0
    async with pool.acquire() as conn:
        await conn.execute("SET search_path = ag_catalog, \"$user\", public")

        for triplet in triplets:
            try:
                # Resolve entities
                subj, subj_merged = await resolve_and_get_node_id(
                    triplet.subject, source_context, get_embedding, pool,
                )
                obj, obj_merged = await resolve_and_get_node_id(
                    triplet.object, source_context, get_embedding, pool,
                )

                # Escape for Cypher
                subj_esc = subj.replace("\\", "\\\\").replace("'", "\\'")
                obj_esc = obj.replace("\\", "\\\\").replace("'", "\\'")
                pred = triplet.predicate.value

                # Use MERGE for nodes (idempotent), CREATE for edge
                cypher = f"""
                    MERGE (s:Entity {{name: '{subj_esc}'}})
                    MERGE (o:Entity {{name: '{obj_esc}'}})
                    CREATE (s)-[:{pred} {{confidence: {triplet.confidence}}}]->(o)
                """

                cypher_sql = f"SELECT * FROM cypher('{graph_name}', $$ {cypher} $$) AS (result agtype);"
                await conn.execute(cypher_sql)
                created += 1

                logger.debug(
                    "Graph edge: %s -[%s]-> %s (merged: s=%s, o=%s)",
                    subj, pred, obj, subj_merged, obj_merged,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to write triplet %s-[%s]->%s: %s",
                    triplet.subject, triplet.predicate.value, triplet.object, exc,
                )

    logger.info("Wrote %d/%d triplet edges to graph", created, len(triplets))
    return created