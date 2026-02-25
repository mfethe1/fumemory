from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memu.temporal_workflow import MemUHealthAndRepairWorkflow


async def run_health_repair_workflow(
    *,
    run_id: str,
    state_dir: str,
    max_attempts: int,
    retry_delays_seconds: list[int],
    health_check,
    repair,
) -> dict[str, Any]:
    """Runtime entrypoint for durable memU health+repair workflow runs.

    Returns a compact JSON-safe summary suitable for cron artifacts.
    """
    workflow = MemUHealthAndRepairWorkflow(
        run_id=run_id,
        health_check=health_check,
        repair=repair,
        state_dir=state_dir,
        max_attempts=max_attempts,
        retry_delays_seconds=retry_delays_seconds,
    )
    state = await workflow.run()
    return {
        "run_id": state.run_id,
        "status": state.status,
        "attempt": state.attempt,
        "repaired": state.repaired,
        "last_error": state.last_error,
        "updated_at": state.updated_at,
    }


def _cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run durable memU health/repair workflow once")
    parser.add_argument("--run-id", default=f"memu-health-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    parser.add_argument("--state-dir", default=".memu-workflow-state")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--retry-delays", default="5,30,120", help="Comma-separated seconds")
    parser.add_argument(
        "--output",
        default="",
        help="Optional path to write JSON summary artifact",
    )
    return parser.parse_args()


async def _always_healthy(_: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "reason": "runtime_smoke"}


async def _noop_repair(_: dict[str, Any]) -> dict[str, Any]:
    return {"repaired": False, "reason": "noop"}


def main() -> int:
    args = _cli()
    retry_delays = [int(chunk.strip()) for chunk in args.retry_delays.split(",") if chunk.strip()]

    summary = asyncio.run(
        run_health_repair_workflow(
            run_id=args.run_id,
            state_dir=args.state_dir,
            max_attempts=args.max_attempts,
            retry_delays_seconds=retry_delays,
            health_check=_always_healthy,
            repair=_noop_repair,
        )
    )

    payload = json.dumps(summary, indent=2, sort_keys=True)
    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload + "\n")
    else:
        print(payload)

    return 0 if summary.get("status") in {"healthy", "recovered"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
