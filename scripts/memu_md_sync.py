import asyncio
import os

import httpx

MEMU_BASE_URL = os.environ.get("MEMU_API_URL", "http://127.0.0.1:8000").rstrip("/")
MEMU_KEY = (os.environ.get("MEMU_API_KEY") or "").strip()

FILES_TO_SYNC = [
    "/home/michael-fethe/.openclaw/workspace/MEMORY.md",
    "/home/michael-fethe/.openclaw/workspace/SKILLBANK.md",
    "/home/michael-fethe/.openclaw/workspace/FAILURES.md",
    "/home/michael-fethe/agent_coordination/BACKLOG.md",
]


async def sync_file(path: str):
    if not os.path.exists(path):
        return
    if not MEMU_KEY:
        raise RuntimeError("MEMU_API_KEY is required for memu_md_sync")

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # Simple chunking by headers
    chunks = content.split("\n#")
    headers = {"X-API-Key": MEMU_KEY, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        for chunk in chunks:
            if not chunk.strip():
                continue
            payload = {
                "content": f"#{chunk}",
                "memory_type": "fact",
                "agent_id": "lenny-sync",
                "metadata": {"source_file": path},
            }
            try:
                resp = await client.post(f"{MEMU_BASE_URL}/memories", json=payload, headers=headers)
                resp.raise_for_status()
            except Exception as e:
                print(f"Error syncing chunk from {path}: {e}")


async def main():
    await asyncio.gather(*[sync_file(p) for p in FILES_TO_SYNC])


if __name__ == "__main__":
    asyncio.run(main())
