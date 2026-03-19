# memu/openclaw_hooks.py
import sys
import os
import logging
import asyncio
import httpx

logger = logging.getLogger("memu.hooks")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
if not logger.handlers:
    logger.addHandler(handler)

MEMU_API_URL = os.environ.get(
    "MEMU_API_URL",
    "https://api-production-86f5.up.railway.app/api/v1/memu",
).rstrip("/")
MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "")


def _headers() -> dict[str, str]:
    return {"X-MemU-Key": MEMU_API_KEY} if MEMU_API_KEY else {}


async def log_action(agent_id: str, action: str, details: dict):
    """Log an agent action to memU using the current compatibility contract."""
    payload = {
        "content": f"Action: {action}",
        "agent_id": agent_id,
        "memory_type": "fact",
        "metadata": {
            "action_type": action,
            "details": details,
            "source": "openclaw_hook",
            "record_type": "user_action",
        },
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{MEMU_API_URL}/add", json=payload, headers=_headers())
            logger.info("Logged action %s for %s (status: %s)", action, agent_id, resp.status_code)
    except Exception as e:
        logger.error("Failed to log action: %s", e)


async def log_search(agent_id: str, query: str, results_summary: str):
    """Log a search artifact to memU using supported memory types."""
    payload = {
        "content": f"Search: {query}\nResult Summary: {results_summary[:500]}...",
        "agent_id": agent_id,
        "memory_type": "fact",
        "metadata": {
            "query": query,
            "type": "web_search",
            "source": "openclaw_hook",
            "record_type": "external",
        },
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"{MEMU_API_URL}/add", json=payload, headers=_headers())
    except Exception as e:
        logger.error("Failed to log search: %s", e)


async def recall(query: str, agent_id: str):
    """Check whether memU already has a strong match for this query."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{MEMU_API_URL}/search",
                json={"query": query, "agent_id": agent_id, "limit": 3},
                headers=_headers(),
            )
            if resp.status_code == 200:
                results = resp.json()
                if isinstance(results, dict):
                    results = results.get("results") or results.get("memories") or []
                if results:
                    top = results[0]
                    score = top.get("final_score", top.get("score", 0))
                    if score > 0.8:
                        memory = top.get("memory", top)
                        return memory.get("content")
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
