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
