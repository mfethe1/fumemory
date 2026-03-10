# memu/openclaw_hooks.py
import sys
import os
import logging
import asyncio
import httpx

# Setup standard logger
logger = logging.getLogger("memu.hooks")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)


def _resolve_memu_base_url() -> str:
    raw = (
        os.environ.get("MEMU_API_URL")
        or os.environ.get("RAILWAY_SERVICE_MEMU_API_URL")
        or "https://memu-api-production.up.railway.app"
    ).rstrip("/")
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    if raw.endswith("/api/v1/memu"):
        return raw
    return f"{raw}/api/v1/memu"


MEMU_API_URL = _resolve_memu_base_url()
MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "")

async def log_action(agent_id: str, action: str, details: dict):
    """Log an agent action to memU."""
    payload = {
        "content": f"Action: {action}",
        "agent_id": agent_id,
        "memory_type": "user_action",
        "metadata": {
            "action_type": action,
            "details": details,
            "source": "openclaw_hook"
        }
    }
    
    headers = {"X-API-Key": MEMU_API_KEY}
    
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{MEMU_API_URL}/memories/async",
                json=payload,
                headers=headers,
            )
            logger.info(f"Logged action {action} for {agent_id} (status: {resp.status_code})")
    except Exception as e:
        logger.error(f"Failed to log action: {e}")

async def log_search(agent_id: str, query: str, results_summary: str):
    """Log a web search to memU."""
    payload = {
        "content": f"Search: {query}\nResult Summary: {results_summary[:500]}...",
        "agent_id": agent_id,
        "memory_type": "external",
        "metadata": {
            "query": query,
            "type": "web_search",
            "source": "openclaw_hook"
        }
    }
    
    headers = {"X-API-Key": MEMU_API_KEY}
    
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{MEMU_API_URL}/memories/async",
                json=payload,
                headers=headers,
            )
    except Exception as e:
        logger.error(f"Failed to log search: {e}")

async def recall(query: str, agent_id: str):
    """Check if we already know this."""
    headers = {"X-API-Key": MEMU_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{MEMU_API_URL}/search",
                json={"query": query, "agent_id": agent_id, "limit": 3},
                headers=headers,
            )
            if resp.status_code == 200:
                results = resp.json()
                if results and results[0]['final_score'] > 0.8:
                    return results[0]['memory']['content']
    except Exception:
        pass
    return None

if __name__ == "__main__":
    if len(sys.argv) > 2:
        cmd = sys.argv[1]
        if cmd == "log-action":
            asyncio.run(log_action("cli", "test_action", {"test": True}))
        elif cmd == "recall":
            res = asyncio.run(recall(sys.argv[2], "cli"))
            print(res if res else "No recall match.")
