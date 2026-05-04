#!/usr/bin/env python3
"""Deployment verification for memU.

Three readiness gates, each a strict superset of the previous:

  Core API Readiness (default):
    GET /health, POST /memories (canonical write), GET /search-text, GET /search/recall

  Federation Readiness (--check-federation):
    Core API + idempotency-keyed evidence write + idempotency replay + searchable memory proof

  Async Readiness (--check-async):
    Core API + POST /memories/async + POST /search/async (Temporal required)

Use --proof-out FILE to emit a machine-readable JSON proof artifact with secrets redacted.
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
from datetime import datetime, timezone


COMPAT_BASE_SUFFIX = "/api/v1/memu"
PRODUCTION_DEFAULT_API_URL = "https://api-production-86f5.up.railway.app/api/v1/memu"


def _default_api_url() -> str:
    return (
        os.environ.get("MEMU_VERIFY_BASE_URL")
        or os.environ.get("MEMU_API_URL")
        or os.environ.get("MEMU_BASE_URL")
        or "http://127.0.0.1:8000"
    ).rstrip("/")


def _api_candidates(api_url: str) -> list[str]:
    requested = (api_url or "").rstrip("/")
    if requested and requested.lower() != "auto":
        return [requested]

    candidates: list[str] = []
    for url in (_default_api_url(), PRODUCTION_DEFAULT_API_URL):
        normalized = url.rstrip("/")
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    return candidates


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
    data = None if json_body is None else json.dumps(json_body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, body


def _compat_mode(api_url: str) -> bool:
    return api_url.rstrip("/").endswith(COMPAT_BASE_SUFFIX)


def _endpoint(api_url: str, path: str, compat_path: str | None = None) -> str:
    suffix = compat_path if (_compat_mode(api_url) and compat_path) else path
    return f"{api_url}{suffix}"


def _check_health(api_url: str) -> tuple[bool, str]:
    health_url = _endpoint(api_url, "/health", "/health")
    print(f"🏥 Checking health at {health_url}...")
    status, body = _request("GET", health_url)
    if status == 200:
        print(f"✅ Healthy: {body}")
        return True, f"status={status}"
    print(f"❌ Unhealthy: {status} - {body}")
    return False, f"status={status} body={body[:120]}"


def _write_sync_memory(api_url: str, api_key: str) -> tuple[str | None, str]:
    print("\n✍️ Testing sync memory write...")
    content = f"Deployment Verification Test {time.time()}"
    payload = {
        "content": content,
        "agent_id": "macklemore-qa",
        "memory_type": "fact",
        "metadata": {"source": "deployment_test", "verified": True},
    }
    status, body = _request(
        "POST",
        _endpoint(api_url, "/memories", "/add"),
        api_key=api_key,
        json_body=payload,
    )
    if status == 200:
        print("✅ Sync write succeeded.")
        return content, "canonical write succeeded"
    print(f"❌ Sync write failed: {status} - {body}")
    return None, f"status={status} body={body[:120]}"


def _verify_search_text(api_url: str, api_key: str, content: str) -> tuple[bool, str]:
    print("\n🔍 Verifying ingestion via /search-text...")
    if _compat_mode(api_url):
        status, body = _request(
            "POST",
            _endpoint(api_url, "/search-text", "/search"),
            api_key=api_key,
            json_body={"query": content, "limit": 1},
        )
    else:
        params = urllib.parse.urlencode({"q": content, "limit": 1})
        status, body = _request("GET", _endpoint(api_url, f"/search-text?{params}", f"/search-text?{params}"), api_key=api_key)
    if status != 200:
        print(f"❌ /search-text failed: {status} - {body}")
        return False, f"status={status} body={body[:120]}"

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        print(f"❌ /search-text response was not JSON: {body}")
        return False, "non-JSON response"

    if isinstance(payload, dict):
        results = payload.get("results") or payload.get("memories") or []
    else:
        results = payload

    if results and content in results[0].get("content", ""):
        print(f"✅ /search-text verified: {results[0]['content'][:80]}...")
        return True, "memory found in results"

    print("❌ /search-text verification failed: newly written memory not found.")
    return False, "memory not found in results"


def _verify_search_recall(api_url: str, api_key: str, content: str) -> tuple[bool, str]:
    print("\n🧠 Verifying retrieval via /search/recall...")
    params = urllib.parse.urlencode({"query": content, "limit": 3})
    attempts = [
        ("GET", _endpoint(api_url, f"/search/recall?{params}", f"/search/recall?{params}"), None, "/search/recall GET"),
        ("POST", _endpoint(api_url, "/search/recall", "/search/recall"), {"query": content, "limit": 3}, "/search/recall POST"),
        ("POST", _endpoint(api_url, "/search", "/search"), {"query": content, "limit": 3}, "/search fallback"),
    ]
    payload = None
    last_error = None
    for method, url, body_json, label in attempts:
        status, body = _request(method, url, api_key=api_key, json_body=body_json)
        if status != 200:
            last_error = f"{label}: {status} - {body}"
            continue
        try:
            payload = json.loads(body)
            break
        except json.JSONDecodeError:
            last_error = f"{label}: non-JSON response {body}"
    else:
        print(f"❌ retrieval verification failed: {last_error}")
        return False, f"all recall attempts failed: {last_error}"

    results = []
    if isinstance(payload, dict):
        results = payload.get("results") or payload.get("memories") or []
    elif isinstance(payload, list):
        results = payload

    if not isinstance(results, list):
        print(f"❌ retrieval returned unexpected payload: {payload}")
        return False, f"unexpected payload shape: {type(payload).__name__}"

    for item in results:
        if content in (item.get("content", "") if isinstance(item, dict) else ""):
            print("✅ Retrieval verified newly written memory.")
            return True, "memory found in recall results"

    print("❌ retrieval verification failed: newly written memory not found.")
    return False, "memory not found in recall results"


def _redact_nats_url(url: str) -> str:
    """Keep scheme+host, redact auth credentials embedded in the URL."""
    if "@" in url:
        scheme_end = url.find("//") + 2
        at_pos = url.rfind("@")
        return url[:scheme_end] + "[REDACTED]@" + url[at_pos + 1:]
    return url


def _check_federation(api_url: str, api_key: str) -> tuple[bool, list[dict]]:
    """Federation Readiness gate.

    Requires NATS_RAILWAY_URL to be present — federation readiness cannot pass
    without NATS. Also proves idempotency-keyed evidence write and replay on
    the memory plane.
    NATS/JetStream publish/consume and directed response are proved by the
    gateway smoke (gateway_federation_smoke.py) and test suite.
    """
    print("\n🔗 Testing Federation Readiness (NATS required + idempotency write + replay + searchable proof)...")
    checks: list[dict] = []

    nats_url = os.environ.get("NATS_RAILWAY_URL", "").strip()
    nats_present = bool(nats_url)
    checks.append({
        "name": "nats_railway_url",
        "passed": nats_present,
        "detail": "NATS_RAILWAY_URL present" if nats_present else "NATS_RAILWAY_URL missing - federation readiness requires NATS",
    })
    if not nats_present:
        print("  ❌ NATS_RAILWAY_URL not set — federation readiness requires NATS.")
        return False, checks

    idempotency_key = f"verify-federation-{int(time.time())}"
    content = f"Federation Readiness Proof {idempotency_key}"
    evidence_payload = {
        "content": content,
        "agent_id": "macklemore-qa",
        "memory_type": "fact",
        "memory_kind": "evidence",
        "idempotency_key": idempotency_key,
        "metadata": {"source": "federation_readiness_test", "gate": "federation"},
    }

    # 1. Idempotency-keyed evidence write
    print("  ✍️  Writing idempotency-keyed evidence memory...")
    status, body = _request(
        "POST",
        _endpoint(api_url, "/memories", "/add"),
        api_key=api_key,
        json_body=evidence_payload,
    )
    write_ok = status == 200
    write_check: dict = {
        "name": "idempotency_write",
        "passed": write_ok,
        "detail": f"status={status}" if not write_ok else "evidence memory written",
    }
    if write_ok:
        try:
            memory_id = json.loads(body).get("id")
            if memory_id:
                write_check["evidence_memory_id"] = str(memory_id)
        except Exception:
            pass
        print("  ✅ Idempotency write succeeded.")
    else:
        print(f"  ❌ Idempotency write failed: {status} - {body}")
    checks.append(write_check)
    if not write_ok:
        return False, checks

    # 2. Idempotency replay — same key must return 409 (deduplication proof)
    print("  🔁 Replaying same idempotency_key to prove deduplication...")
    status2, body2 = _request(
        "POST",
        _endpoint(api_url, "/memories", "/add"),
        api_key=api_key,
        json_body=evidence_payload,
    )
    replay_ok = status2 == 409
    checks.append({
        "name": "idempotency_replay",
        "passed": replay_ok,
        "detail": "409 dedup confirmed" if replay_ok else f"expected 409 got {status2}",
    })
    if replay_ok:
        print("  ✅ Idempotency replay confirmed (409 dedup).")
    else:
        print(f"  ❌ Idempotency replay failed: expected 409, got {status2} - {body2}")

    # 3. Searchable memory proof via /search/recall
    print("  🧠 Proving searchable memory via /search/recall...")
    search_ok, search_detail = _verify_search_recall(api_url, api_key, content)
    checks.append({
        "name": "federation_searchable_proof",
        "passed": search_ok,
        "detail": search_detail,
    })

    overall = all(c["passed"] for c in checks)
    if overall:
        print("✅ Federation Readiness gate passed.")
    else:
        print("❌ Federation Readiness gate failed.")
    return overall, checks


def _check_async(api_url: str, api_key: str) -> tuple[bool, list[dict]]:
    print("\n🚀 Testing async endpoints (Temporal required)...")
    checks: list[dict] = []

    ingest_payload = {
        "content": f"Async Deployment Verification Test {time.time()}",
        "agent_id": "macklemore-qa",
        "memory_type": "fact",
        "metadata": {"source": "deployment_test", "mode": "async"},
    }
    status, body = _request("POST", _endpoint(api_url, "/memories/async", "/memories/async"), api_key=api_key, json_body=ingest_payload)
    ingest_ok = status == 200
    checks.append({"name": "async_ingest", "passed": ingest_ok, "detail": f"status={status}" if not ingest_ok else "accepted"})
    if not ingest_ok:
        print(f"❌ Async ingestion failed: {status} - {body}")
        return False, checks

    search_payload = {"query": "deployment_test", "agent_id": "macklemore-qa", "limit": 1}
    status, body = _request("POST", _endpoint(api_url, "/search/async", "/search/async"), api_key=api_key, json_body=search_payload)
    search_ok = status == 200
    checks.append({"name": "async_search", "passed": search_ok, "detail": f"status={status}" if not search_ok else "accepted"})
    if not search_ok:
        print(f"❌ Async search failed: {status} - {body}")
        return False, checks

    print("✅ Async endpoints accepted requests.")
    return True, checks


def _build_proof(
    gate: str,
    api_url: str,
    api_key: str,
    checks: list[dict],
    overall: bool,
) -> dict:
    """Build a machine-readable proof artifact with secrets redacted."""
    proof: dict = {
        "schema_version": 1,
        "gate": gate,
        "api_url": api_url,
        "api_key": "[REDACTED]",
        "timestamp_utc": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checks": checks,
        "overall": "pass" if overall else "fail",
    }
    if gate == "federation":
        gateway_id = os.environ.get("GATEWAY_ID", "").strip()
        nats_url = os.environ.get("NATS_RAILWAY_URL", "").strip()
        if gateway_id:
            proof["gateway_id"] = gateway_id
        if nats_url:
            proof["nats_railway_url"] = _redact_nats_url(nats_url)
        proof["evidence_memory_ids"] = [
            c["evidence_memory_id"] for c in checks if "evidence_memory_id" in c
        ]
    return proof


def _write_proof(proof: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(proof, f, indent=2)
    print(f"\n📄 Proof artifact written to {path}")


def _verify_single(
    api_url: str,
    api_key: str,
    check_federation: bool,
    check_async: bool,
    proof_out: str | None,
) -> bool:
    gate = "core"
    all_checks: list[dict] = []

    health_ok, health_detail = _check_health(api_url)
    all_checks.append({"name": "health", "passed": health_ok, "detail": health_detail})
    if not health_ok:
        if proof_out:
            _write_proof(_build_proof(gate, api_url, api_key, all_checks, False), proof_out)
        return False

    content, write_detail = _write_sync_memory(api_url, api_key)
    all_checks.append({"name": "canonical_write", "passed": content is not None, "detail": write_detail})
    if not content:
        if proof_out:
            _write_proof(_build_proof(gate, api_url, api_key, all_checks, False), proof_out)
        return False

    search_ok, search_detail = _verify_search_text(api_url, api_key, content)
    all_checks.append({"name": "search_text", "passed": search_ok, "detail": search_detail})
    if not search_ok:
        if proof_out:
            _write_proof(_build_proof(gate, api_url, api_key, all_checks, False), proof_out)
        return False

    recall_ok, recall_detail = _verify_search_recall(api_url, api_key, content)
    all_checks.append({"name": "search_recall", "passed": recall_ok, "detail": recall_detail})
    if not recall_ok:
        if proof_out:
            _write_proof(_build_proof(gate, api_url, api_key, all_checks, False), proof_out)
        return False

    if check_federation:
        gate = "federation"
        fed_ok, fed_checks = _check_federation(api_url, api_key)
        all_checks.extend(fed_checks)
        if not fed_ok:
            if proof_out:
                _write_proof(_build_proof(gate, api_url, api_key, all_checks, False), proof_out)
            return False

    if check_async:
        if gate == "core":
            gate = "async"
        async_ok, async_checks = _check_async(api_url, api_key)
        all_checks.extend(async_checks)
        if not async_ok:
            if proof_out:
                _write_proof(_build_proof(gate, api_url, api_key, all_checks, False), proof_out)
            return False

    print(f"\n🎉 Deployment verification complete: SUCCESS ({api_url})")
    if proof_out:
        _write_proof(_build_proof(gate, api_url, api_key, all_checks, True), proof_out)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a memU deployment without mutating infra config.")
    parser.add_argument("--api-url", default="auto", help="Base URL for memU API, or 'auto' to try local then production")
    parser.add_argument("--api-key", default=None, help="memU API key (defaults to env/secrets lookup)")
    parser.add_argument("--check-federation", action="store_true", help="Also verify Federation Readiness gate (idempotency write/replay, searchable memory proof)")
    parser.add_argument("--check-async", action="store_true", help="Also verify Temporal-backed async endpoints")
    parser.add_argument("--proof-out", default=None, metavar="FILE", help="Write machine-readable JSON proof artifact to FILE (secrets redacted)")
    args = parser.parse_args()

    api_key = _resolve_api_key(args.api_key)
    failures: list[str] = []
    for api_url in _api_candidates(args.api_url):
        print(f"\n=== Verifying {api_url} ===")
        if _verify_single(api_url, api_key, args.check_federation, args.check_async, args.proof_out):
            return 0
        failures.append(api_url)

    print(f"\n❌ Deployment verification failed for: {', '.join(failures)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
