import os
import httpx
import asyncio

MEMU_URL = os.environ.get(
    "MEMU_API_URL",
    "https://api-production-86f5.up.railway.app/api/v1/memu",
).rstrip("/")
MEMU_KEY = os.environ.get("MEMU_API_KEY", "")

if not MEMU_KEY:
    secrets_path = os.path.expanduser("~/.openclaw/secrets/memu_api_key")
    if os.path.exists(secrets_path):
        with open(secrets_path, "r") as f:
            MEMU_KEY = f.read().strip()

FILES_TO_SYNC = [
    "/home/michael-fethe/.openclaw/workspace/MEMORY.md",
    "/home/michael-fethe/.openclaw/workspace/SKILLBANK.md",
    "/home/michael-fethe/.openclaw/workspace/FAILURES.md",
    "/home/michael-fethe/agent_coordination/BACKLOG.md",
]


async def sync_file(path: str):
    if not os.path.exists(path):
        return

    with open(path, "r") as f:
        content = f.read()

    chunks = content.split("\n#")
    headers = {"X-MemU-Key": MEMU_KEY, "Content-Type": "application/json"}

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
                await client.post(f"{MEMU_URL}/add", json=payload, headers=headers)
            except Exception as e:
                print(f"Error syncing chunk from {path}: {e}")


async def main():
    if not MEMU_KEY:
        raise SystemExit("MEMU_API_KEY not set and ~/.openclaw/secrets/memu_api_key missing")
    await asyncio.gather(*[sync_file(p) for p in FILES_TO_SYNC])


if __name__ == "__main__":
    asyncio.run(main())
