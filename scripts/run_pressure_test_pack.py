#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
ARTIFACTS.mkdir(parents=True, exist_ok=True)


def run(cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def tcp_preflight(host: str, port: int, timeout_s: float = 3.0) -> bool:
    py = (
        "import socket,sys;"
        f"s=socket.socket();s.settimeout({timeout_s});"
        f"\ntry:\n s.connect(('{host}',{port}));print('ok');sys.exit(0)\n"
        "except Exception:\n print('fail');sys.exit(1)"
    )
    rc, out, _ = run([sys.executable, "-c", py])
    return rc == 0 and out.strip() == "ok"


def main() -> int:
    ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    artifact = ARTIFACTS / f"pressure_pack_{ts}.json"

    nats_url = os.getenv("NATS_URL") or os.getenv("RAILWAY_NATS_URL")
    temporal_addr = os.getenv("TEMPORAL_ADDRESS") or os.getenv("RAILWAY_TEMPORAL_ADDRESS")

    preflight = {
        "nats": {"configured": bool(nats_url)},
        "temporal": {"configured": bool(temporal_addr)},
    }

    # hard gate for integration mode
    integration_mode = os.getenv("INTEGRATION_MODE", "1") == "1"
    errors: list[str] = []

    if not nats_url:
        errors.append("missing NATS_URL/RAILWAY_NATS_URL")
    else:
        # parse host:port from nats://host:port
        try:
            hostport = nats_url.split("//", 1)[1]
            host, port_s = hostport.rsplit(":", 1)
            preflight["nats"]["reachable"] = tcp_preflight(host, int(port_s))
            if not preflight["nats"]["reachable"]:
                errors.append(f"nats unreachable: {host}:{port_s}")
        except Exception:
            errors.append("invalid NATS_URL format")

    if temporal_addr:
        try:
            host, port_s = temporal_addr.rsplit(":", 1)
            preflight["temporal"]["reachable"] = tcp_preflight(host, int(port_s))
            if not preflight["temporal"]["reachable"]:
                errors.append(f"temporal unreachable: {host}:{port_s}")
        except Exception:
            errors.append("invalid TEMPORAL_ADDRESS format")
    else:
        errors.append("missing TEMPORAL_ADDRESS/RAILWAY_TEMPORAL_ADDRESS")

    if integration_mode and errors:
        data = {
            "ts": ts,
            "overall_ok": False,
            "status": "preflight_failed",
            "preflight": preflight,
            "errors": errors,
            "scenarios": {},
        }
        artifact.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(artifact)
        return 2

    env = os.environ.copy()
    scenarios = {}

    # Scenario 1: replay-after-crash (temporal tests)
    rc, out, err = run([sys.executable, "-m", "pytest", "-q", "tests/test_temporal_runtime_integration.py"], env=env)
    scenarios["replay_after_crash"] = {"rc": rc, "ok": rc == 0, "stdout": out[-4000:], "stderr": err[-2000:]}

    # Scenario 2: 429/degraded path
    rc, out, err = run([sys.executable, "-m", "pytest", "-q", "tests/test_session_hook_temporal.py"], env=env)
    scenarios["rate_limit_degraded"] = {"rc": rc, "ok": rc == 0, "stdout": out[-4000:], "stderr": err[-2000:]}

    # Scenario 3: dual-claim contention (must run, no skip expected)
    rc, out, err = run([sys.executable, "scripts/lane_lock_dual_claim_drill.py"], env=env)
    dual_ok = rc == 0 and '"ok": true' in out.lower()
    scenarios["dual_claim"] = {"rc": rc, "ok": dual_ok, "stdout": out[-4000:], "stderr": err[-2000:]}

    overall_ok = all(v.get("ok") for v in scenarios.values())

    data = {
        "ts": ts,
        "overall_ok": overall_ok,
        "status": "pass" if overall_ok else "fail",
        "preflight": preflight,
        "errors": errors,
        "scenarios": scenarios,
    }
    artifact.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(artifact)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
