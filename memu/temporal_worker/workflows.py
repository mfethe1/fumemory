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
class NextIntentWorkflow:
    @workflow.run
    async def run(self, user_id: str, signal: str, limit: int = 3) -> list[dict]:
        predictions = await workflow.execute_activity(
            "infer_next_intent_activity",
            args=[user_id, signal, limit],
            start_to_close_timeout=timedelta(seconds=60),
        )
        await workflow.execute_activity(
            "log_audit",
            args=["NEXT_INTENT_PREDICTED", user_id, {"signal": signal, "count": len(predictions)}],
            start_to_close_timeout=timedelta(seconds=30),
        )
        return predictions