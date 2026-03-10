#!/usr/bin/env python3
"""Run memU duplicate cleanup from the CLI."""

from __future__ import annotations

import argparse
import os
import sys

import requests


def main() -> int:
    parser = argparse.ArgumentParser(description="Collapse duplicate memU memories")
    parser.add_argument("--base-url", default=os.environ.get("MEMU_BASE_URL", "http://localhost:8000"))
    parser.add_argument("--api-key", default=os.environ.get("MEMU_API_KEY", "memu-dev-key"))
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    response = requests.post(
        f"{args.base_url.rstrip('/')}/memories/dedupe",
        headers={"X-API-Key": args.api_key},
        params={"limit": args.limit, "dry_run": str(args.dry_run).lower()},
        timeout=60,
    )
    response.raise_for_status()
    print(response.json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
