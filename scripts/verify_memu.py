#!/usr/bin/env python3
"""Simple memU health probe for agent hosts."""

from __future__ import annotations

import os
import sys

import httpx

DEFAULT_BASE_URL = "https://api-production-86f5.up.railway.app"


def verify_memu(base_url: str | None = None, timeout: float = 5.0) -> bool:
    root = (base_url or os.environ.get("MEMU_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    url = f"{root}/api/v1/memu/health"
    try:
        response = httpx.get(url, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        memories = data.get("total_memories") or data.get("memory_count") or 0
        db_status = data.get("db") or data.get("status") or "unknown"
        print(f"✅ memU is ONLINE — url={url} db={db_status} memories={memories}")
        return True
    except Exception as exc:
        print(f"❌ memU probe failed — url={url} error={exc}")
        return False


if __name__ == "__main__":
    success = verify_memu()
    raise SystemExit(0 if success else 1)
