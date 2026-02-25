from __future__ import annotations

import pytest

from memu.temporal_runtime import run_health_repair_workflow
from memu.temporal_workflow import MemUHealthAndRepairWorkflow


@pytest.mark.asyncio
async def test_temporal_runtime_crash_resume_flow(tmp_path):
    calls = {"health": 0, "repair": 0}

    async def crashing_health(_: dict[str, object]):
        calls["health"] += 1
        if calls["health"] == 1:
            raise RuntimeError("simulated crash")
        return {"ok": True}

    async def repair(_: dict[str, object]):
        calls["repair"] += 1
        return {"repaired": True}

    workflow = MemUHealthAndRepairWorkflow(
        run_id="resume-1",
        health_check=crashing_health,
        repair=repair,
        state_dir=str(tmp_path),
        max_attempts=3,
        retry_delays_seconds=[0],
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        await workflow.run()

    summary = await run_health_repair_workflow(
        run_id="resume-1",
        state_dir=str(tmp_path),
        max_attempts=3,
        retry_delays_seconds=[0],
        health_check=crashing_health,
        repair=repair,
    )

    assert summary["status"] == "healthy"
    assert summary["attempt"] == 2
    assert calls["health"] == 2
