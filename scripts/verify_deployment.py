#!/usr/bin/env python3
"""Deployment verification for memU.

Default mode verifies the core API only:
- GET /health
- POST /memories
- POST /search-text

Async/Temporal checks are optional and only run when --check-async is passed.
This avoids false negatives on Railway when Temporal is not part of the current deploy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


WRITE_PATHS = ("/upsert", "/memories")
SEARCH_PROBES = (
    ("POST", "/search", lambda q: {"query": q, "limit": 1}),
    ("POST", "/search-text", lambda q: {"query": q, "limit": 1}),
    ("GET", "/search-text", lambda q: None),
)


def _default_api_url() -> str:
    return (
        os.environ.get("MEMU_VERIFY_BASE_URL")
        or os.environ.get("MEMU_API_URL")
        or os.environ.get("MEMU_BASE_URL")
        or "http://127.0.0.1:8000"
    ).rstrip("/")


def _resolve_api_key(explicit: str | None) -> str:
    if explicit:
        return explicit.strip()

    env_key = (os.environ.get("MEMU_API_KEY") or "").strip()
    if env_key:
        return env_key

    secret_path = os.path.expanduser("~/.openclaw/secrets/memu_api_key")
    try:
        with open(secret_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        print("❌ MEMU_API_KEY not found in env or secrets.")
        sys.exit(1)


def _request(method: str, url: str, *, api_key: str | None = None, json_body: dict | None = None, timeout: int = 15):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-MemU-Key"] = api_key
        headers["X-API-Key"] = api_key
    data = None if json_body is None else json.dumps(json_body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, body


def _parse_json(body: str):
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _check_health(api_url: str) -> bool:
    print(f"🏥 Checking health at {api_url}/health...")
    status, body = _request("GET", f"{api_url}/health")
    if status == 200:
        print(f"✅ Healthy: {body}")
        return True
    print(f"❌ Unhealthy: {status} - {body}")
    return False


def _write_sync_memory(api_url: str, api_key: str) -> str | None:
    print("\n✍️ Testing sync memory write...")
    content = f"Deployment Verification Test {time.time()}"
    payload = {
        "content": content,
        "agent_id": "macklemore-qa",
        "memory_type": "fact",
        "metadata": {"source": "deployment_test", "verified": True},
    }
    for path in WRITE_PATHS:
        status, body = _request("POST", f"{api_url}{path}", api_key=api_key, json_body=payload)
        if status == 200:
            print(f"✅ Sync write succeeded via {path}.")
            return content
    print(f"❌ Sync write failed on {', '.join(WRITE_PATHS)}: {status} - {body}")
    return None


def _verify_ingestion(api_url: str, api_key: str, content: str) -> bool:
    print("\n🔍 Verifying ingestion via search endpoint...")
    for method, path, payload_builder in SEARCH_PROBES:
        if method == "GET":
            params = urllib.parse.urlencode({"q": content, "query": content, "limit": 1})
            status, body = _request(method, f"{api_url}{path}?{params}", api_key=api_key)
        else:
            status, body = _request(method, f"{api_url}{path}", api_key=api_key, json_body=payload_builder(content))

        if status != 200:
            continue

        parsed = _parse_json(body)
        if parsed is None:
            print(f"❌ Verification response was not JSON: {body}")
            return False

        if isinstance(parsed, dict):
            results = parsed.get("results") or parsed.get("memories") or []
        else:
            results = parsed

        if results and content in results[0].get("content", ""):
            print(f"✅ Verified via {path}. Found memory: {results[0]['content'][:80]}...")
            return True

    print("❌ Verification failed: newly written memory not found.")
    return False


def _check_recall(api_url: str, api_key: str, query: str) -> bool:
    print("\n🧠 Verifying /search/recall...")
    status, body = _request("POST", f"{api_url}/search/recall", api_key=api_key, json_body={"query": query, "limit": 1})
    if status in {404, 405}:
        print("⚠️ Recall endpoint not deployed on this target; core search verification already passed.")
        return True
    if status != 200:
        print(f"❌ Recall query failed: {status} - {body}")
        return False

    results = _parse_json(body)
    if results is None:
        print(f"❌ Recall response was not JSON: {body}")
        return False

    if isinstance(results, list):
        print(f"✅ Recall endpoint responded with {len(results)} result(s).")
        return True

    print(f"❌ Recall response shape was unexpected: {body}")
    return False


def _check_async(api_url: str, api_key: str) -> bool:
    print("\n🚀 Testing async endpoints (Temporal required)...")

    ingest_payload = {
        "content": f"Async Deployment Verification Test {time.time()}",
        "agent_id": "macklemore-qa",
        "memory_type": "observation",
        "metadata": {"source": "deployment_test", "mode": "async"},
    }
    status, body = _request("POST", f"{api_url}/memories/async", api_key=api_key, json_body=ingest_payload)
    if status != 200:
        print(f"❌ Async ingestion failed: {status} - {body}")
        return False

    search_payload = {"query": "deployment_test", "agent_id": "macklemore-qa", "limit": 1}
    status, body = _request("POST", f"{api_url}/search/async", api_key=api_key, json_body=search_payload)
    if status != 200:
        print(f"❌ Async search failed: {status} - {body}")
        return False

    print("✅ Async endpoints accepted requests.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a memU deployment without mutating infra config.")
    parser.add_argument("--api-url", default=_default_api_url(), help="Base URL for memU API")
    parser.add_argument("--api-key", default=None, help="memU API key (defaults to env/secrets lookup)")
    parser.add_argument("--check-async", action="store_true", help="Also verify Temporal-backed async endpoints")
    args = parser.parse_args()

    api_url = args.api_url.rstrip("/")
    api_key = _resolve_api_key(args.api_key)

    if not _check_health(api_url):
        return 1

    content = _write_sync_memory(api_url, api_key)
    if not content or not _verify_ingestion(api_url, api_key, content):
        return 1

    if not _check_recall(api_url, api_key, content):
        return 1

    if args.check_async and not _check_async(api_url, api_key):
        return 1

    print("\n🎉 Deployment verification complete: SUCCESS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
