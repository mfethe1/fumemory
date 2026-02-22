"""fumemory swarm health check — run from any agent to verify full stack."""
import asyncio
import json
import sys
import socket

try:
    import nats
except ImportError:
    print("[!!] nats-py not installed. Run: pip install nats-py")
    sys.exit(1)

NATS_LOCAL = "nats://100.76.63.58:4222"
NATS_RAILWAY = "nats://gondola.proxy.rlwy.net:22393"
BRIDGE_URL = "http://127.0.0.1:8001"
GLASSBOX_URL = "http://localhost:3001"
MEMU_URL = "https://api-production-86f5.up.railway.app/api/v1/memu/health"


def tcp_check(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        s = socket.create_connection((host, port), timeout)
        s.close()
        return True
    except Exception:
        return False


async def nats_check(url: str, label: str) -> bool:
    try:
        nc = await nats.connect(url, connect_timeout=5)
        info = nc._server_info
        print(f"[OK] NATS ({label:8s}) @ {url} | v{info.get('version','?')} | JS={'on' if info.get('jetstream') else 'off'}")
        await nc.drain()
        return True
    except Exception as e:
        print(f"[!!] NATS ({label:8s}) @ {url} | {e}")
        return False


def http_check(url: str, label: str) -> bool:
    try:
        import urllib.request
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = resp.read()
            if resp.status == 200:
                try:
                    d = json.loads(data)
                    extra = ""
                    if "total_tasks" in d:
                        extra = f" | {d['total_tasks']} tasks, {d['total_events']} events"
                    elif "active_node" in d:
                        extra = f" | active: {d['active_node']}"
                    print(f"[OK] {label:18s} @ {url}{extra}")
                except json.JSONDecodeError:
                    print(f"[OK] {label:18s} @ {url} | {len(data)} bytes")
                return True
            else:
                print(f"[!!] {label:18s} @ {url} | HTTP {resp.status}")
                return False
    except Exception as e:
        print(f"[!!] {label:18s} @ {url} | {e}")
        return False


async def main():
    print("=" * 50)
    print("   fumemory SWARM OS — HEALTH CHECK")
    print("=" * 50)
    print()

    results = []

    # NATS checks
    results.append(await nats_check(NATS_LOCAL, "local"))
    results.append(await nats_check(NATS_RAILWAY, "railway"))

    # HTTP checks
    results.append(http_check(f"{BRIDGE_URL}/api/stats", "Bridge (stats)"))
    results.append(http_check(f"{BRIDGE_URL}/api/cluster/status", "Bridge (cluster)"))
    results.append(http_check(GLASSBOX_URL, "Glass Box UI"))
    results.append(http_check(MEMU_URL, "memU API"))

    print()
    passed = sum(results)
    total = len(results)
    if passed == total:
        print(f"ALL GREEN: {passed}/{total} checks passed")
    else:
        print(f"DEGRADED: {passed}/{total} checks passed")
        print("Run with verbose logging or check individual services.")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
