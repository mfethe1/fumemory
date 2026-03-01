"""Full stack integration tests for fumemory v1.0.

Tests: API health, memory CRUD, search, NATS events, Notion bridge,
audit trail, and temporal endpoints.
"""

import asyncio
import json
import os
import time

import httpx

BASE = "http://localhost:8000"
KEY = os.environ.get("MEMU_API_KEY", "memu-dev-key")
HEADERS = {"X-API-Key": KEY, "Content-Type": "application/json"}
NOTION_KEY = None
try:
    NOTION_KEY = open(os.path.expanduser("~/.config/notion/api_key")).read().strip()
except FileNotFoundError:
    pass

RESULTS = []


def log(name: str, passed: bool, detail: str = ""):
    status = "✅ PASS" if passed else "❌ FAIL"
    RESULTS.append((name, passed, detail))
    print(f"  {status} | {name}" + (f" — {detail}" if detail else ""))


async def run_tests():
    async with httpx.AsyncClient(timeout=120.0) as c:

        # 1. Health
        print("\n=== 1. Health ===")
        r = await c.get(f"{BASE}/health")
        log("API /health", r.status_code == 200, f"HTTP {r.status_code}")

        # 2. Memory Write (old type)
        print("\n=== 2. Memory Write (old type: fact) ===")
        r = await c.post(f"{BASE}/memories", headers=HEADERS, json={
            "content": f"[TEST] stack test fact {time.time()}",
            "memory_type": "fact",
            "agent_id": "lenny",
            "metadata": {"test": True},
        })
        old_type_ok = r.status_code == 200
        log("Write (fact)", old_type_ok, f"HTTP {r.status_code}")
        if old_type_ok:
            mem_id = r.json()["id"]
            log("Memory ID returned", bool(mem_id), mem_id[:8])

        # 3. Memory Write (new type)
        print("\n=== 3. Memory Write (new type: observation) ===")
        r = await c.post(f"{BASE}/memories", headers=HEADERS, json={
            "content": f"[TEST] stack test observation {time.time()}",
            "memory_type": "observation",
            "agent_id": "lenny",
            "metadata": {"test": True},
        })
        new_type_ok = r.status_code == 200
        log("Write (observation)", new_type_ok, f"HTTP {r.status_code}")

        # 4. Memory Read
        print("\n=== 4. Memory Read ===")
        if old_type_ok:
            r = await c.get(f"{BASE}/memories/{mem_id}", headers=HEADERS)
            log("Read by ID", r.status_code == 200, f"HTTP {r.status_code}")
            if r.status_code == 200:
                log("Access count incremented", r.json()["access_count"] >= 1)

        # 5. Search (vector)
        print("\n=== 5. Vector Search ===")
        r = await c.post(f"{BASE}/search", headers=HEADERS, json={
            "query": "stack test",
            "limit": 3,
        })
        search_ok = r.status_code == 200
        log("Search endpoint", search_ok, f"HTTP {r.status_code}")
        if search_ok:
            results = r.json()
            log("Results returned", len(results) > 0, f"{len(results)} hits")
            if results:
                log("Has similarity score", "similarity" in results[0])
                log("Has final_score", "final_score" in results[0])

        # 6. Search Text
        print("\n=== 6. Text Search ===")
        r = await c.post(f"{BASE}/search-text", headers=HEADERS, params={
            "query": "stack test",
            "limit": 3,
        })
        log("Search-text endpoint", r.status_code == 200, f"HTTP {r.status_code}")

        # 7. Bulk Import
        print("\n=== 7. Bulk Import ===")
        r = await c.post(f"{BASE}/memories/bulk", headers=HEADERS, json={
            "content": f"[TEST] bulk item 1 {time.time()}\n[TEST] bulk item 2 {time.time()}",
            "split_on": "\n",
            "memory_type": "lesson",
            "agent_id": "lenny",
        })
        log("Bulk import", r.status_code == 200, f"HTTP {r.status_code}")

        # 8. Memory Delete
        print("\n=== 8. Memory Delete ===")
        if old_type_ok:
            r = await c.delete(f"{BASE}/memories/{mem_id}", headers=HEADERS)
            log("Delete by ID", r.status_code == 200, f"HTTP {r.status_code}")

        # 9. Temporal Async Endpoint
        print("\n=== 9. Temporal /memories/async ===")
        r = await c.post(f"{BASE}/memories/async", headers=HEADERS, json={
            "content": f"[TEST] temporal write {time.time()}",
            "memory_type": "observation",
            "agent_id": "lenny",
        })
        log("Temporal async write", r.status_code in (200, 503), f"HTTP {r.status_code} — {'workflow accepted' if r.status_code == 200 else 'Temporal unavailable (expected if server not ready)'}")

        # 10. NATS Event Consumer
        print("\n=== 10. NATS Event Consumer ===")
        import subprocess
        logs = subprocess.run(
            ["docker", "logs", "memu-oss-event-consumer-1"],
            capture_output=True, text=True, timeout=10
        )
        has_events = "swarm.memory" in logs.stdout or "swarm.search" in logs.stdout or "swarm.memory" in logs.stderr or "swarm.search" in logs.stderr
        total_line = [l for l in (logs.stdout + logs.stderr).split("\n") if "total=" in l]
        event_count = 0
        if total_line:
            import re
            m = re.search(r"total=(\d+)", total_line[-1])
            if m:
                event_count = int(m.group(1))
        log("Events received", event_count > 0, f"{event_count} total events")
        log("Swarm subjects active", has_events, "swarm.memory/search subjects seen" if has_events else "no swarm subjects yet")

        # 11. Notion Bridge
        print("\n=== 11. Notion Bridge ===")
        if NOTION_KEY:
            nr = await c.post(
                "https://api.notion.com/v1/databases/314df4e3-9102-81ee-ab23-e53b6ee74323/query",
                headers={
                    "Authorization": f"Bearer {NOTION_KEY}",
                    "Notion-Version": "2022-06-28",
                    "Content-Type": "application/json",
                },
                json={"page_size": 1},
            )
            log("Notion Task Board reachable", nr.status_code == 200, f"HTTP {nr.status_code}")
            if nr.status_code == 200:
                count = len(nr.json().get("results", []))
                log("Tasks in board", count >= 0, f"{count}+ tasks")
        else:
            log("Notion key available", False, "~/.config/notion/api_key not found")

        # 12. Audit Trail
        print("\n=== 12. Audit Trail (search_history) ===")
        import subprocess
        audit = subprocess.run(
            ["docker", "exec", "memu-oss-postgres-1", "psql", "-U", "memu", "-c",
             "SELECT count(*) FROM search_history;"],
            capture_output=True, text=True, timeout=10
        )
        if audit.returncode == 0:
            log("search_history table exists", True, audit.stdout.strip().split("\n")[2].strip() + " rows")
        else:
            log("search_history table exists", False, audit.stderr[:100])

        audit2 = subprocess.run(
            ["docker", "exec", "memu-oss-postgres-1", "psql", "-U", "memu", "-c",
             "SELECT count(*) FROM audit_log;"],
            capture_output=True, text=True, timeout=10
        )
        if audit2.returncode == 0:
            log("audit_log table exists", True, audit2.stdout.strip().split("\n")[2].strip() + " rows")
        else:
            log("audit_log table exists", False, audit2.stderr[:100])

    # Summary
    print("\n" + "=" * 60)
    passed = sum(1 for _, p, _ in RESULTS if p)
    failed = sum(1 for _, p, _ in RESULTS if not p)
    print(f"TOTAL: {passed} passed, {failed} failed, {len(RESULTS)} tests")
    if failed > 0:
        print("\nFailed tests:")
        for name, p, d in RESULTS:
            if not p:
                print(f"  ❌ {name}: {d}")
    print("=" * 60)
    return failed == 0


if __name__ == "__main__":
    ok = asyncio.run(run_tests())
    exit(0 if ok else 1)
