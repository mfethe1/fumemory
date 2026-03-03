import os
import hashlib
import json
import httpx
import asyncio

MEMU_URL = os.environ.get("MEMU_API_URL", "https://api-production-86f5.up.railway.app/api/v1/memu")
MEMU_KEY = os.environ.get("MEMU_API_KEY", "memu_YTwYX33NfIWYU33B1ixyOGA_JajxPohd3ftPWH4pcCc")

FILES_TO_SYNC = [
    "/home/michael-fethe/.openclaw/workspace/MEMORY.md",
    "/home/michael-fethe/.openclaw/workspace/SKILLBANK.md",
    "/home/michael-fethe/.openclaw/workspace/FAILURES.md",
    "/home/michael-fethe/agent_coordination/BACKLOG.md"
]

async def sync_file(path):
    if not os.path.exists(path): return
    with open(path, 'r') as f:
        content = f.read()
    
    # Simple chunking by headers
    chunks = content.split('\n#')
    headers = {"X-MemU-Key": MEMU_KEY, "Content-Type": "application/json"}
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        for chunk in chunks:
            if not chunk.strip(): continue
            payload = {
                "content": f"#{chunk}",
                "memory_type": "fact",
                "agent_id": "lenny-sync",
                "metadata": {"source_file": path}
            }
            try:
                await client.post(f"{MEMU_URL}/memories", json=payload, headers=headers)
            except Exception as e:
                print(f"Error syncing chunk from {path}: {e}")

async def main():
    await asyncio.gather(*[sync_file(p) for p in FILES_TO_SYNC])

if __name__ == "__main__":
    asyncio.run(main())
