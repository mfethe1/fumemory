from typing import Any, Optional
from memu.client import MemUClient


class MemUDependencies:
    """Dependencies for PydanticAI agents to access memU."""
    def __init__(self, client: MemUClient, agent_id: Optional[str] = None):
        self.client = client
        self.agent_id = agent_id


def search_memory_tool(context: Any, query: str, limit: int = 5) -> str:
    """Tool for PydanticAI to search memories."""
    deps: MemUDependencies = context.deps
    results = deps.client.search(query=query, limit=limit, agent_id=deps.agent_id)
    if not results:
        return "No relevant memories found."
    return "\n".join([f"- {r.content}" for r in results])


def add_memory_tool(context: Any, content: str, memory_type: str = "fact") -> str:
    """Tool for PydanticAI to add a memory."""
    deps: MemUDependencies = context.deps
    deps.client.add(content=content, memory_type=memory_type, agent_id=deps.agent_id)
    return "Memory successfully added."


def core_mem_append_tool(context: Any, text: str) -> str:
    """Append text to your Working_Context core memory block."""
    deps: MemUDependencies = context.deps
    resp = deps.client._client.post(
        f"/api/v1/memu/core-memory/{deps.agent_id}/Working_Context/append",
        json={"text": text},
    )
    resp.raise_for_status()
    data = resp.json()
    return f"Appended to Working_Context ({data.get('tokens', '?')} tokens total)"


def core_mem_replace_tool(context: Any, old_text: str, new_text: str) -> str:
    """Replace specific text in your Working_Context core memory block."""
    deps: MemUDependencies = context.deps
    resp = deps.client._client.post(
        f"/api/v1/memu/core-memory/{deps.agent_id}/Working_Context/replace",
        json={"old_text": old_text, "new_text": new_text},
    )
    resp.raise_for_status()
    return "Replaced text in Working_Context"


def page_archival_tool(context: Any, query: str, limit: int = 3) -> str:
    """Search archival memory and page results into working context."""
    deps: MemUDependencies = context.deps
    resp = deps.client._client.post(
        f"/api/v1/memu/core-memory/{deps.agent_id}/page-archival",
        json={"query": query, "limit": limit},
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("paged", [])
    if not results:
        return "No archival memories matched your query."
    return "\n".join([f"- {r['content']}" for r in results])
