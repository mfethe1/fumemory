#!/usr/bin/env python3
"""Consolidated pressure test pack for Lane C night run.

Runs three scenarios in one pass and writes a single evidence artifact:
- replay-after-crash (temporal workflow resume)
- dual-claim (lane lock contention)
- 429/degraded (session hook backoff/error behavior)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))


@dataclass
class ScenarioResult:
    name: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _run_pytest(nodeid: str) -> ScenarioResult:
    cmd = [shutil.which("pytest") or "pytest", "-q", *nodeid.split()]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ},
            timeout=45,
        )
        returncode = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stdout = exc.stdout or ""
        stderr = (exc.stderr or "") + "\nPYTEST_TIMEOUT_45S"
    return ScenarioResult(
        name=nodeid,
        command=cmd,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def run_pressure_pack() -> dict[str, Any]:
    scenarios = [
        ("replay-after-crash", "tests/test_temporal_runtime_integration.py::test_temporal_runtime_crash_resume_flow"),
        ("dual-claim", "tests/test_lane_lock_claim_integration.py::test_lane_lock_real_task_claim_contention"),
        (
            "429/degraded",
            (
                "tests/test_session_hook_temporal.py::test_session_hook_retry_429_then_recover_with_deterministic_injector "
                "tests/test_session_hook_temporal.py::test_session_hook_falls_back_to_degraded_error_after_429_exhausted"
            ),
        ),
    ]

    results: dict[str, dict[str, object]] = {}
    overall_ok = True

    for label, nodeid in scenarios:
        result = _run_pytest(nodeid)
        key = label
        results[key] = {
            "ok": result.ok,
            "command": " ".join(result.command),
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
        overall_ok = overall_ok and result.ok

        if not result.ok:
            results[key]["fail_hint"] = "scenario failed; check stderr/stdout for detail"

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scenario_count": len(scenarios),
        "overall_ok": overall_ok,
        "scenarios": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run consolidated pressure test pack")
    parser.add_argument(
        "--artifact-dir",
        default=str(ROOT / "artifacts"),
        help="Directory to store pressure-pack evidence JSON",
    )
    args = parser.parse_args()

    summary = run_pressure_pack()
    artifact_dir = Path(args.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"pressure_pack_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    artifact_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(json.dumps(summary, indent=2))
    print(f"Artifact: {artifact_path}")

    return 0 if summary["overall_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
