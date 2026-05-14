# memu/temporal_worker/activities.py
import hashlib
import json
import os
from typing import Any

import asyncpg
from temporalio import activity
import httpx

# Progressive Skill Loading: Lazy-load FastEmbed only when needed
# This reduces initial memory footprint and context window size
_fastembed_model: Any | None = None


def get_db_url():
    return os.environ.get("DATABASE_URL")


# --- Embedding helpers (OpenAI-compatible + local fallback) ---

# EMBEDDING_API_BASE is canonical; EMBEDDING_BASE_URL is a deprecated alias.
_embedding_api_base_canonical = os.environ.get("EMBEDDING_API_BASE", "")
_embedding_base_url_alias = os.environ.get("EMBEDDING_BASE_URL", "")
if not _embedding_api_base_canonical and _embedding_base_url_alias:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "EMBEDDING_BASE_URL is a deprecated alias for EMBEDDING_API_BASE. "
        "Update your deployment configuration to use EMBEDDING_API_BASE instead."
    )
    _embedding_api_base_canonical = _embedding_base_url_alias

EMBEDDING_API_BASE = _embedding_api_base_canonical or "https://api.openai.com"
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIMS = int(os.environ.get("EMBEDDING_DIMS", "1536"))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")


async def _embedding_from_http(text: str) -> list[float] | None:
    """Try OpenAI-compatible endpoint (/v1/embeddings), including Ollama compatibility."""
    # Only attempt remote embedding call for explicit providers.
    if not OPENAI_API_KEY and "ollama" not in EMBEDDING_API_BASE:
        return None

    base = EMBEDDING_API_BASE.rstrip("/")
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
async def store_memory(req_dict: dict, embedding: list[float] | None) -> dict:
    """Store memory preserving all canonical evidence fields.

    Accepts a req_dict with canonical fields so the async path never silently
    hardcodes memory_type or drops provenance.  Returns a dict with:
      memory_id         — UUID string of the persisted record
      idempotency_status — "new" | "exact_replay"
    """
    from memu.schema_contract import compute_canonical_payload_hash

    content = req_dict.get("content", "")
    agent_id = req_dict.get("agent_id", "system")
    memory_type = req_dict.get("memory_type", "observation")
    memory_kind = req_dict.get("memory_kind", "learning")
    idempotency_key = req_dict.get("idempotency_key") or None
    salience_score = float(req_dict.get("salience_score", 0.5))
    metadata: dict = dict(req_dict.get("metadata") or {})
    tenant_id = req_dict.get("tenant_id", "00000000-0000-0000-0000-000000000001")
    parent_id = req_dict.get("parent_id") or None

    # Ensure allowed_roles is in metadata for ABAC
    if "allowed_roles" not in metadata:
        metadata["allowed_roles"] = req_dict.get("allowed_roles") or ["*"]

    is_evidence = (memory_kind == "evidence")
    canonical_hash: str | None = None

    if is_evidence and idempotency_key:
        canonical_hash = compute_canonical_payload_hash(
            content=content,
            memory_type=memory_type,
            agent_id=agent_id,
            metadata=metadata,
        )

    # One-shot connection — acceptable for Temporal activity isolation
    conn = await asyncpg.connect(get_db_url())
    try:
        # Idempotency check for evidence writes
        if is_evidence and idempotency_key:
            existing = await conn.fetchrow(
                """
                SELECT id, canonical_payload_hash FROM memories
                WHERE tenant_id = $1::uuid AND idempotency_key = $2
                LIMIT 1
                """,
                tenant_id,
                idempotency_key,
            )
            if existing:
                stored_hash = existing["canonical_payload_hash"]
                if stored_hash == canonical_hash:
                    return {"memory_id": str(existing["id"]), "idempotency_status": "exact_replay"}
                # Conflict — let the caller decide; still return the existing ID
                return {
                    "memory_id": str(existing["id"]),
                    "idempotency_status": "conflict",
                }

        row = await conn.fetchrow(
            """
            INSERT INTO memories (
                content, agent_id, metadata, memory_type, memory_kind,
                salience_score, tenant_id, idempotency_key, canonical_payload_hash,
                parent_id, embedding
            )
            VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7::uuid, $8, $9, $10, $11::vector)
            RETURNING id
            """,
            content,
            agent_id,
            json.dumps(metadata),
            memory_type,
            memory_kind,
            salience_score,
            tenant_id,
            idempotency_key,
            canonical_hash,
            parent_id,
            str(embedding) if embedding else None,
        )
        return {"memory_id": str(row["id"]), "idempotency_status": "new"}
    finally:
        await conn.close()


@activity.defn
async def search_memory(query: str, agent_id: str, embedding: list[float] | None) -> list:
    """Execute search query."""
    # One-shot connection — acceptable for Temporal activity isolation
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
    # One-shot connection — acceptable for Temporal activity isolation
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
    # One-shot connection — acceptable for Temporal activity isolation
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


def _dream_idempotency_key(agent_id: str, source_episode_ids: list[str]) -> str:
    """Generate a deterministic MD5 hash from sorted source episode IDs.

    This ensures that Temporal retries of DreamConsolidationWorkflow
    produce the same key and can detect duplicate rules.
    """
    sorted_ids = sorted(str(eid) for eid in source_episode_ids if eid)
    # Use ::: as delimiter — cannot appear in UUIDs or agent IDs,
    # preventing collisions like agent="a" + episodes=["b,c"] vs ["b","c"]
    payload = f"{agent_id}:::{','.join(sorted_ids)}"
    return hashlib.md5(payload.encode()).hexdigest()


@activity.defn
async def store_dream_rule(agent_id: str, rule_text: str, source_episode_ids: list[str]) -> str:
    """Store a synthesized rule as a high-salience semantic memory (idempotent).

    Uses a deterministic MD5 idempotency key derived from the sorted source
    episode IDs. If a rule with the same key already exists, returns the
    existing ID instead of creating a duplicate.
    """
    idempotency_key = _dream_idempotency_key(agent_id, source_episode_ids)

    # One-shot connection — acceptable for Temporal activity isolation
    conn = await asyncpg.connect(get_db_url())
    try:
        # Generate embedding for the rule
        embedding = await generate_embedding(rule_text)

        metadata = json.dumps({
            "source": "dream_consolidation",
            "source_episode_ids": source_episode_ids,
            "idempotency_key": idempotency_key,
        })

        # Single-statement atomic upsert: eliminates TOCTOU race between
        # SELECT and INSERT that could create duplicates on Temporal retries.
        # Uses partial unique index uq_memories_idempotency_key (migration 017).
        row = await conn.fetchrow(
            """
            INSERT INTO memories (content, agent_id, memory_type, salience_score,
                                  metadata, embedding)
            VALUES ($1, $2, 'reflection', 0.90,
                    $3::jsonb, $4::vector)
            ON CONFLICT ((metadata->>'idempotency_key'))
                WHERE metadata->>'idempotency_key' IS NOT NULL
            DO NOTHING
            RETURNING id
            """,
            rule_text,
            agent_id,
            metadata,
            str(embedding) if embedding else None,
        )

        if row:
            # New row inserted
            rule_id = str(row["id"])
        else:
            # DO NOTHING fired — fetch existing ID
            existing = await conn.fetchrow(
                """
                SELECT id FROM memories
                WHERE agent_id = $1
                  AND metadata->>'idempotency_key' = $2
                LIMIT 1
                """,
                agent_id,
                idempotency_key,
            )
            rule_id = str(existing["id"]) if existing else "unknown"
            activity.logger.info(
                "Dream rule already exists (idempotency_key=%s), returning existing ID %s",
                idempotency_key, rule_id,
            )

        # Create DERIVED_FROM edges in Apache AGE (best-effort, idempotent MERGE)
        try:
            await conn.execute("SET search_path = ag_catalog, \"$user\", public")
            for ep_id in source_episode_ids:
                if ep_id:
                    cypher_sql = f"""
                    SELECT * FROM cypher('memu_graph', $$
                        MERGE (rule:Memory {{id: '{rule_id}'}})
                        MERGE (ep:Memory {{id: '{ep_id}'}})
                        MERGE (rule)-[:DERIVED_FROM]->(ep)
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
    # One-shot connection — acceptable for Temporal activity isolation
    conn = await asyncpg.connect(get_db_url())
    try:
        # Filter out None/empty IDs
        valid_ids = [eid for eid in episode_ids if eid]
        if not valid_ids:
            return 0
        await conn.execute(
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


# ---------------------------------------------------------------------------
# GDPR "Scorched Earth" Hard-Deletion Activities
# ---------------------------------------------------------------------------


@activity.defn
async def gdpr_delete_kv_memory(agent_id: str) -> bool:
    """Purge all core memory KV entries for an agent.

    GDPR Article 17 "Right to Erasure" — permanently deletes all KV blocks
    for the specified agent from the NATS KV core memory bucket.
    """
    try:
        from memu.cluster import NATSClusterManager
        from memu.core_memory import CoreMemoryManager

        cluster = NATSClusterManager()
        await cluster.connect()
        try:
            mgr = CoreMemoryManager(cluster.jetstream)
            deleted = await mgr.delete_agent(agent_id)
            activity.logger.info("GDPR: deleted %d KV entries for agent %s", deleted, agent_id)
            return True
        finally:
            await cluster.close()
    except Exception as e:
        activity.logger.error("GDPR KV delete failed for %s: %s", agent_id, e)
        return False


@activity.defn
async def gdpr_delete_vector_memory(tenant_id: str, user_id: str) -> int:
    """Hard-delete all vector memories for a user from PostgreSQL.

    GDPR Article 17 — bypasses bitemporal retention rules.
    """
    try:
        # One-shot connection — acceptable for Temporal activity isolation
        conn = await asyncpg.connect(get_db_url())
        try:
            result = await conn.execute(
                "DELETE FROM memories WHERE agent_id = $1",
                user_id,
            )
            count = int(result.split()[-1]) if result else 0
            activity.logger.info("GDPR: deleted %d vector rows for user %s", count, user_id)
            return count
        finally:
            await conn.close()
    except Exception as e:
        activity.logger.error("GDPR vector delete failed for %s: %s", user_id, e)
        return 0


@activity.defn
async def gdpr_delete_graph_memory(tenant_id: str, user_id: str) -> bool:
    """Hard-delete all knowledge graph entities for a user from Apache AGE.

    GDPR Article 17 — removes all nodes with matching user_id from the
    tenant-specific graph namespace.
    """
    try:
        graph_name = f"tenant_{tenant_id}" if tenant_id else "memu_default"
        # One-shot connection — acceptable for Temporal activity isolation
        conn = await asyncpg.connect(get_db_url())
        try:
            await conn.execute("LOAD 'age';")
            await conn.execute("SET search_path = ag_catalog, '$user', public;")
            await conn.execute(
                f"SELECT * FROM ag_catalog.cypher('{graph_name}', "
                f"$$ MATCH (n {{user_id: '{user_id}'}}) DETACH DELETE n $$) AS (v agtype);"
            )
            activity.logger.info("GDPR: deleted graph entities for user %s in graph %s", user_id, graph_name)
            return True
        finally:
            await conn.close()
    except Exception as e:
        activity.logger.error("GDPR graph delete failed for %s: %s", user_id, e)
        return False


# ---------------------------------------------------------------------------
# Graph Healing Activities (Asynchronous Entity Deduplication)
# ---------------------------------------------------------------------------


@activity.defn
async def detect_duplicate_graph_nodes(tenant_id: str) -> list[dict]:
    """Query the tenant's AGE graph for all entity nodes, group by vector
    similarity (> 0.94), and return clusters of likely-duplicate entities.

    Each cluster is a list of ``{"name": str, "id": str}`` dicts representing
    graph nodes that an LLM should verify as identical entities.

    This is the *detection* phase only — no mutations are performed.
    """
    graph_name = f"tenant_{tenant_id}" if tenant_id else "memu_default"
    SIMILARITY_THRESHOLD = 0.94
    clusters: list[dict] = []

    try:
        conn = await asyncpg.connect(get_db_url())
        try:
            # Load AGE extension and set search path
            await conn.execute("LOAD 'age';")
            await conn.execute("SET search_path = ag_catalog, '$user', public;")

            # Fetch all entity nodes from the tenant graph
            try:
                rows = await conn.fetch(
                    f"SELECT * FROM ag_catalog.cypher('{graph_name}', "
                    f"$$ MATCH (n) RETURN n.id AS id, n.name AS name $$) "
                    f"AS (id agtype, name agtype);"
                )
            except Exception:
                activity.logger.info(
                    "Graph '%s' does not exist or has no nodes — skipping healing",
                    graph_name,
                )
                return []

            if len(rows) < 2:
                return []

            # Build node list with embeddings from the memories table
            # For each node name, find its embedding in the memories table
            nodes: list[dict] = []
            for row in rows:
                name = str(row["name"]).strip('"') if row["name"] else None
                node_id = str(row["id"]).strip('"') if row["id"] else None
                if name and node_id:
                    # Look up embedding from memories table by content similarity
                    emb_row = await conn.fetchrow(
                        "SELECT embedding FROM memories "
                        "WHERE content ILIKE $1 AND embedding IS NOT NULL LIMIT 1",
                        f"%{name[:50]}%",
                    )
                    embedding = None
                    if emb_row and emb_row["embedding"]:
                        emb = emb_row["embedding"]
                        if isinstance(emb, str):
                            try:
                                import json as _json
                                embedding = _json.loads(emb.replace("(", "[").replace(")", "]"))
                            except Exception:
                                pass
                    nodes.append({"id": node_id, "name": name, "embedding": embedding})

            # Greedy clustering by cosine similarity > threshold
            from memu.memory_agent import cosine_similarity

            clustered_ids: set[str] = set()
            for i, node_a in enumerate(nodes):
                if node_a["id"] in clustered_ids or not node_a.get("embedding"):
                    continue
                cluster = [{"id": node_a["id"], "name": node_a["name"]}]
                clustered_ids.add(node_a["id"])

                for node_b in nodes[i + 1:]:
                    if node_b["id"] in clustered_ids or not node_b.get("embedding"):
                        continue
                    sim = cosine_similarity(node_a["embedding"], node_b["embedding"])
                    if sim > SIMILARITY_THRESHOLD:
                        cluster.append({"id": node_b["id"], "name": node_b["name"]})
                        clustered_ids.add(node_b["id"])

                if len(cluster) >= 2:
                    clusters.append({
                        "tenant_id": tenant_id,
                        "graph_name": graph_name,
                        "nodes": cluster,
                        "count": len(cluster),
                    })

            activity.logger.info(
                "Graph healing: found %d duplicate clusters in graph '%s'",
                len(clusters), graph_name,
            )
        finally:
            await conn.close()
    except Exception as e:
        activity.logger.error("Graph healing detection failed for %s: %s", tenant_id, e)

    return clusters


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
GDPRDeleteKVMemoryActivity = gdpr_delete_kv_memory
GDPRDeleteVectorMemoryActivity = gdpr_delete_vector_memory
GDPRDeleteGraphMemoryActivity = gdpr_delete_graph_memory
DetectDuplicateGraphNodesActivity = detect_duplicate_graph_nodes
