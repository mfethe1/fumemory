# memu/temporal_worker/activities.py
import json
import os
from typing import Any

import asyncpg
from temporalio import activity
import httpx

# Local cache for FastEmbed fallback (kept module-private for worker determinism)
_fastembed_model: Any | None = None


def get_db_url():
    return os.environ.get("DATABASE_URL")


# --- Embedding helpers (OpenAI-compatible + local fallback) ---

EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
EMBEDDING_DIMS = int(os.environ.get("EMBEDDING_DIMS", "384"))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")


async def _embedding_from_http(text: str) -> list[float] | None:
    """Try OpenAI-compatible endpoint (/v1/embeddings), including Ollama compatibility."""
    # Only attempt remote embedding call for explicit providers.
    if not OPENAI_API_KEY and "ollama" not in EMBEDDING_BASE_URL:
        return None

    base = EMBEDDING_BASE_URL.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if OPENAI_API_KEY:
        headers["Authorization"] = f"Bearer {OPENAI_API_KEY}"

    async def _try(url: str, payload: dict) -> list[float] | None:
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if "data" in data and data["data"]:
                    emb = data["data"][0].get("embedding")
                    if emb is not None:
                        return emb
                if "embedding" in data:
                    return data["embedding"]
        except Exception as e:
            activity.logger.warning("Remote embedding request failed (%s): %s", url, e)
        return None

    emb = await _try(
        f"{base}/v1/embeddings",
        {
            "input": text,
            "model": EMBEDDING_MODEL,
            "dimensions": EMBEDDING_DIMS,
        },
    )
    if emb is not None and len(emb) == EMBEDDING_DIMS:
        return emb

    emb = await _try(
        f"{base}/api/embeddings",
        {
            "model": EMBEDDING_MODEL,
            "prompt": text,
        },
    )
    if emb is not None and len(emb) == EMBEDDING_DIMS:
        return emb
    if emb is not None:
        activity.logger.warning(
            "Embedding dim mismatch from remote provider: got=%d expected=%d",
            len(emb),
            EMBEDDING_DIMS,
        )

    return None


async def _embedding_from_fastembed(text: str) -> list[float] | None:
    global _fastembed_model

    if _fastembed_model is None:
        try:
            from fastembed import TextEmbedding

            _fastembed_model = TextEmbedding()
        except Exception as e:
            activity.logger.warning("FastEmbed unavailable: %s", e)
            return None

    try:
        embeddings = list(_fastembed_model.embed([text]))
        emb = embeddings[0].tolist()
        if len(emb) == EMBEDDING_DIMS:
            return emb
        activity.logger.warning(
            "FastEmbed dim mismatch: got=%d expected=%d",
            len(emb),
            EMBEDDING_DIMS,
        )
    except Exception as e:
        activity.logger.warning("FastEmbed generation failed: %s", e)
    return None


@activity.defn
async def generate_embedding(text: str) -> list[float] | None:
    """Generate embedding vector (activity wrapper)."""
    # Mirror API behavior: prefer remote embedding API, then local FastEmbed fallback.
    remote = await _embedding_from_http(text)
    if remote is not None:
        return remote
    return await _embedding_from_fastembed(text)


@activity.defn
async def store_memory(content: str, agent_id: str, metadata: dict, embedding: list[float] | None) -> str:
    """Store memory and return ID."""
    conn = await asyncpg.connect(get_db_url())
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO memories (content, agent_id, metadata, memory_type, embedding)
            VALUES ($1, $2, $3, 'user_action', $4::vector)
            RETURNING id
            """,
            content,
            agent_id,
            json.dumps(metadata),
            str(embedding) if embedding else None,
        )
        return str(row["id"])
    finally:
        await conn.close()


@activity.defn
async def search_memory(query: str, agent_id: str, embedding: list[float] | None) -> list:
    """Execute search query."""
    conn = await asyncpg.connect(get_db_url())
    try:
        if embedding:
            rows = await conn.fetch(
                """
                SELECT id, content, 1 - (embedding <=> $1::vector) as similarity
                FROM memories 
                WHERE agent_id = $2
                ORDER BY embedding <=> $1::vector
                LIMIT 5
                """,
                str(embedding),
                agent_id,
            )
        else:
            rows = await conn.fetch(
                "SELECT id, content, 0.0 as similarity FROM memories WHERE content ILIKE $1 AND agent_id = $2 LIMIT 5",
                f"%{query}%",
                agent_id,
            )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


@activity.defn
async def log_audit(action_type: str, agent_id: str, details: dict):
    """Log structured audit event."""
    conn = await asyncpg.connect(get_db_url())
    try:
        await conn.execute(
            """
            INSERT INTO audit_log (action_type, agent_id, details, created_at)
            VALUES ($1, $2, $3, NOW())
            """,
            action_type,
            agent_id,
            json.dumps(details),
        )
        return True
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Dream Consolidation Activities (Phase 3)
# ---------------------------------------------------------------------------

LLM_BASE_URL = os.environ.get("PROFILER_LLM_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("PROFILER_LLM_MODEL", "gpt-4o-mini")


@activity.defn
async def fetch_recent_episodes(agent_id: str, hours: int) -> list[dict]:
    """Fetch episodic memories from the last N hours."""
    conn = await asyncpg.connect(get_db_url())
    try:
        rows = await conn.fetch(
            """
            SELECT id, content, memory_type, metadata, salience_score,
                   created_at, updated_at
            FROM memories
            WHERE agent_id = $1
              AND created_at > NOW() - ($2 || ' hours')::interval
              AND searchable = TRUE
              AND memory_type IN ('observation', 'fact', 'failure', 'user_action', 'external')
            ORDER BY created_at DESC
            LIMIT 50
            """,
            agent_id,
            str(hours),
        )
        return [dict(r) for r in rows]
    except Exception as e:
        activity.logger.warning("fetch_recent_episodes failed: %s", e)
        return []
    finally:
        await conn.close()


@activity.defn
async def synthesize_dream_rules(agent_id: str, episodes: list[dict]) -> dict:
    """Use LLM to synthesize generalized rules from episodic memories."""
    if not OPENAI_API_KEY:
        return {"rules": []}

    episode_texts = []
    for ep in episodes[:20]:  # Limit to 20 episodes
        episode_texts.append(f"- [{ep.get('memory_type', 'unknown')}] {ep.get('content', '')[:300]}")

    prompt = f"""Analyze these recent episodic memories from agent '{agent_id}' and synthesize
generalized architectural rules or patterns. Extract wisdom from specific events.

Episodes:
{chr(10).join(episode_texts)}

Output JSON: {{"rules": ["rule 1 text", "rule 2 text", ...]}}
Only include clear, actionable rules. Max 5 rules."""

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {OPENAI_API_KEY}"}
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "You synthesize wisdom from episodic memories into generalized rules."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 500,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{LLM_BASE_URL}/chat/completions", headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return json.loads(content)
    except Exception as e:
        activity.logger.warning("Dream synthesis LLM call failed: %s", e)
        return {"rules": []}


@activity.defn
async def store_dream_rule(agent_id: str, rule_text: str, source_episode_ids: list[str]) -> str:
    """Store a synthesized rule as a high-salience semantic memory."""
    conn = await asyncpg.connect(get_db_url())
    try:
        # Generate embedding for the rule
        embedding = await generate_embedding(rule_text)

        row = await conn.fetchrow(
            """
            INSERT INTO memories (content, agent_id, memory_type, salience_score,
                                  metadata, embedding)
            VALUES ($1, $2, 'reflection', 0.90,
                    $3::jsonb, $4::vector)
            RETURNING id
            """,
            rule_text,
            agent_id,
            json.dumps({
                "source": "dream_consolidation",
                "source_episode_ids": source_episode_ids,
            }),
            str(embedding) if embedding else None,
        )
        rule_id = str(row["id"])

        # Create DERIVED_FROM edges in Apache AGE (best-effort)
        try:
            await conn.execute("SET search_path = ag_catalog, \"$user\", public")
            for ep_id in source_episode_ids:
                if ep_id:
                    cypher_sql = f"""
                    SELECT * FROM cypher('memu_graph', $$
                        MERGE (rule:Memory {{id: '{rule_id}'}})
                        MERGE (ep:Memory {{id: '{ep_id}'}})
                        CREATE (rule)-[:DERIVED_FROM]->(ep)
                    $$) AS (result agtype);
                    """
                    await conn.execute(cypher_sql)
        except Exception as e:
            activity.logger.warning("DERIVED_FROM edge creation failed: %s", e)

        return rule_id
    finally:
        await conn.close()


@activity.defn
async def mark_episodes_consolidated(episode_ids: list[str]) -> int:
    """Mark source episodes as non-searchable (retain provenance, exclude from RAG)."""
    if not episode_ids:
        return 0
    conn = await asyncpg.connect(get_db_url())
    try:
        # Filter out None/empty IDs
        valid_ids = [eid for eid in episode_ids if eid]
        if not valid_ids:
            return 0
        result = await conn.execute(
            """
            UPDATE memories SET searchable = FALSE, updated_at = NOW()
            WHERE id = ANY($1::uuid[])
            """,
            valid_ids,
        )
        return len(valid_ids)
    except Exception as e:
        activity.logger.warning("mark_episodes_consolidated failed: %s", e)
        return 0
    finally:
        await conn.close()


@activity.defn
async def inject_prospective_memory(agent_id: str, intent: str) -> bool:
    """Inject a prospective memory reminder into agent's Working_Context via NATS KV."""
    try:
        from memu.cluster import NATSClusterManager
        from memu.core_memory import Block, CoreMemoryManager

        cluster = NATSClusterManager()
        await cluster.connect()
        try:
            mgr = CoreMemoryManager(cluster.jetstream)
            await mgr.ensure_bucket()

            reminder = f"[URGENT REMINDER: {intent}]"
            # CAS-safe prepend to Working_Context
            entry = await mgr.get_block(agent_id, Block.WORKING_CONTEXT)
            if entry:
                new_content = f"{reminder}\n{entry.content}"
                await mgr.update_block(
                    agent_id, Block.WORKING_CONTEXT, new_content,
                    entry.revision, caller="agent",
                )
            else:
                await mgr.update_block(
                    agent_id, Block.WORKING_CONTEXT, reminder,
                    0, caller="agent",
                )
            return True
        finally:
            await cluster.close()
    except Exception as e:
        activity.logger.warning("inject_prospective_memory failed: %s", e)
        return False


# Export for worker registration
GenerateEmbeddingActivity = generate_embedding
StoreMemoryActivity = store_memory
SearchMemoryActivity = search_memory
LogAuditActivity = log_audit
FetchRecentEpisodesActivity = fetch_recent_episodes
SynthesizeDreamRulesActivity = synthesize_dream_rules
StoreDreamRuleActivity = store_dream_rule
MarkEpisodesConsolidatedActivity = mark_episodes_consolidated
InjectProspectiveMemoryActivity = inject_prospective_memory
