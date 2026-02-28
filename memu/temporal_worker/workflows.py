from datetime import timedelta
from temporalio import workflow

# Import activity definition interface
with workflow.unsafe.imports_passed_through():
    from memu.temporal_worker.activities import (
        StoreMemoryActivity,
        SearchMemoryActivity,
        LogAuditActivity,
    )

@workflow.defn
class MemoryIngestionWorkflow:
    @workflow.run
    async def run(self, content: str, agent_id: str, metadata: dict) -> str:
        # 1. Store the raw memory
        memory_id = await workflow.execute_activity(
            StoreMemoryActivity,
            args=[content, agent_id, metadata],
            start_to_close_timeout=timedelta(seconds=10),
        )

        # 2. Log audit trail (Policy: Log Everything)
        await workflow.execute_activity(
            LogAuditActivity,
            args=["MEMORY_STORED", agent_id, {"memory_id": memory_id}],
            start_to_close_timeout=timedelta(seconds=5),
        )

        return memory_id

@workflow.defn
class MemorySearchWorkflow:
    @workflow.run
    async def run(self, query: str, agent_id: str) -> list[dict]:
        # 1. Log search intent (Audit)
        await workflow.execute_activity(
            LogAuditActivity,
            args=["SEARCH_INTENT", agent_id, {"query": query}],
            start_to_close_timeout=timedelta(seconds=5),
        )

        # 2. Execute search
        results = await workflow.execute_activity(
            SearchMemoryActivity,
            args=[query, agent_id],
            start_to_close_timeout=timedelta(seconds=10),
        )

        # 3. Log search outcome (Audit)
        await workflow.execute_activity(
            LogAuditActivity,
            args=["SEARCH_COMPLETE", agent_id, {"count": len(results)}],
            start_to_close_timeout=timedelta(seconds=5),
        )

        return results
