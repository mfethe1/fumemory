from datetime import timedelta
from temporalio import workflow

@workflow.defn
class MemoryIngestionWorkflow:
    @workflow.run
    async def run(self, content: str, agent_id: str, metadata: dict) -> str:
        # 0. Generate embedding (durable)
        embedding = await workflow.execute_activity(
            "generate_embedding",
            args=[content],
            start_to_close_timeout=timedelta(seconds=60),
        )

        # 1. Store memory
        memory_id = await workflow.execute_activity(
            "store_memory",
            args=[content, agent_id, metadata, embedding],
            start_to_close_timeout=timedelta(seconds=120),
        )

        # 2. Log audit
        await workflow.execute_activity(
            "log_audit",
            args=["MEMORY_STORED", agent_id, {"memory_id": memory_id}],
            start_to_close_timeout=timedelta(seconds=30),
        )

        return memory_id


@workflow.defn
class MemorySearchWorkflow:
    @workflow.run
    async def run(self, query: str, agent_id: str) -> list[dict]:
        # 0. Generate embedding
        embedding = await workflow.execute_activity(
            "generate_embedding",
            args=[query],
            start_to_close_timeout=timedelta(seconds=60),
        )

        # 1. Log intent
        await workflow.execute_activity(
            "log_audit",
            args=["SEARCH_INTENT", agent_id, {"query": query}],
            start_to_close_timeout=timedelta(seconds=30),
        )

        # 2. Execute search
        results = await workflow.execute_activity(
            "search_memory",
            args=[query, agent_id, embedding],
            start_to_close_timeout=timedelta(seconds=60),
        )

        # 3. Log outcome
        await workflow.execute_activity(
            "log_audit",
            args=["SEARCH_COMPLETE", agent_id, {"count": len(results)}],
            start_to_close_timeout=timedelta(seconds=30),
        )
        return results


@workflow.defn
class DreamConsolidationWorkflow:
    """Hippocampal replay — consolidates episodic memories into semantic rules.

    Triggered when an agent has been idle > 2 hours (no tasks).
    Steps:
      1. Query recent episodic memories (last 48h)
      2. LLM synthesizes generalized architectural rules
      3. Write rules as high-salience Semantic memories
      4. Link to source episodes with DERIVED_FROM edges in Apache AGE
      5. Mark source episodes searchable=False (retain provenance, exclude from RAG)
    """

    @workflow.run
    async def run(self, agent_id: str) -> dict:
        # 1. Fetch recent episodic memories
        episodes = await workflow.execute_activity(
            "fetch_recent_episodes",
            args=[agent_id, 48],  # last 48 hours
            start_to_close_timeout=timedelta(seconds=60),
        )

        if not episodes or len(episodes) < 2:
            return {"status": "skipped", "reason": "insufficient_episodes", "count": len(episodes or [])}

        # 2. Synthesize generalized rules via LLM
        synthesis = await workflow.execute_activity(
            "synthesize_dream_rules",
            args=[agent_id, episodes],
            start_to_close_timeout=timedelta(seconds=120),
        )

        if not synthesis or not synthesis.get("rules"):
            return {"status": "skipped", "reason": "no_rules_synthesized"}

        # 3. Store each rule as a high-salience semantic memory
        stored_rule_ids = []
        for rule in synthesis["rules"]:
            rule_id = await workflow.execute_activity(
                "store_dream_rule",
                args=[agent_id, rule, [ep.get("id") for ep in episodes]],
                start_to_close_timeout=timedelta(seconds=60),
            )
            stored_rule_ids.append(rule_id)

        # 4. Mark source episodes as non-searchable (retain provenance)
        await workflow.execute_activity(
            "mark_episodes_consolidated",
            args=[[ep.get("id") for ep in episodes]],
            start_to_close_timeout=timedelta(seconds=60),
        )

        # 5. Log audit
        await workflow.execute_activity(
            "log_audit",
            args=["DREAM_CONSOLIDATION", agent_id, {
                "episodes_consolidated": len(episodes),
                "rules_created": len(stored_rule_ids),
                "rule_ids": stored_rule_ids,
            }],
            start_to_close_timeout=timedelta(seconds=30),
        )

        return {
            "status": "completed",
            "episodes_consolidated": len(episodes),
            "rules_created": len(stored_rule_ids),
            "rule_ids": stored_rule_ids,
        }


@workflow.defn
class ProspectiveMemoryWorkflow:
    """Prospective Memory — remembering to act in the future.

    Sleeps until a trigger condition is met (time-based), then
    forcefully injects the intent into the agent's Working_Context.
    """

    @workflow.run
    async def run(self, agent_id: str, intent: str, sleep_seconds: int) -> dict:
        # Sleep until trigger time
        await workflow.sleep(timedelta(seconds=sleep_seconds))

        # Inject intent into agent's Working_Context
        result = await workflow.execute_activity(
            "inject_prospective_memory",
            args=[agent_id, intent],
            start_to_close_timeout=timedelta(seconds=30),
        )

        # Log audit
        await workflow.execute_activity(
            "log_audit",
            args=["PROSPECTIVE_MEMORY_TRIGGERED", agent_id, {
                "intent": intent,
                "sleep_seconds": sleep_seconds,
            }],
            start_to_close_timeout=timedelta(seconds=30),
        )

        return {"status": "triggered", "agent_id": agent_id, "intent": intent}


@workflow.defn
class GDPRScrubWorkflow:
    """Permanently delete ALL data for a user across all storage layers.

    GDPR Article 17 "Right to Erasure" — bypasses bitemporal retention.
    Deletes from: NATS KV (core memory), PostgreSQL (vector memories),
    Apache AGE (knowledge graph entities).
    """

    @workflow.run
    async def run(self, tenant_id: str, user_id: str, agent_id: str) -> dict:
        # Execute all three deletions. Each is idempotent and independently retried.
        kv_ok = await workflow.execute_activity(
            "gdpr_delete_kv_memory",
            args=[agent_id],
            start_to_close_timeout=timedelta(seconds=30),
        )
        rows_deleted = await workflow.execute_activity(
            "gdpr_delete_vector_memory",
            args=[tenant_id, user_id],
            start_to_close_timeout=timedelta(seconds=30),
        )
        graph_ok = await workflow.execute_activity(
            "gdpr_delete_graph_memory",
            args=[tenant_id, user_id],
            start_to_close_timeout=timedelta(seconds=30),
        )
        return {
            "kv_deleted": kv_ok,
            "vector_rows_deleted": rows_deleted,
            "graph_deleted": graph_ok,
        }