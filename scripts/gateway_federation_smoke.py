#!/usr/bin/env python3
"""Smoke verifier for shared Railway-backed NATS federation."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memu.gateway_federation import FederationConfig, results_to_json, run_smoke_checks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify a gateway can join the shared Railway NATS federation."
    )
    parser.add_argument(
        "--skip-memu",
        action="store_true",
        help="Skip memU write/search smoke checks and test NATS transport only.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON output instead of a human-readable summary.",
    )
    return parser.parse_args()


async def _main() -> int:
    args = parse_args()
    cfg = FederationConfig.from_env()
    results = await run_smoke_checks(cfg, run_memu_check=not args.skip_memu)
    payload = results_to_json(results, gateway_id=cfg.gateway_id, nats_railway_url=cfg.nats_railway_url)

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for item in payload["results"]:
            status = "PASS" if item["ok"] else "FAIL"
            print(f"[{status}] {item['name']}: {item['detail']}")
        print(f"overall_ok={payload['ok']}")

    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_main()))
    except Exception as exc:  # pragma: no cover - CLI failure path
        print(f"gateway federation smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
