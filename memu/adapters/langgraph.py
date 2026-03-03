from typing import Optional, Any
from memu.client import MemUClient

try:
    from langchain_core.tools import tool

    def get_memu_tools(client: MemUClient, agent_id: Optional[str] = None) -> list[Any]:
        """Get LangChain/LangGraph tools for memU memory interaction."""
        
        @tool
        def search_memory(query: str, limit: int = 5) -> str:
            """Search long-term memory for relevant context or facts based on a semantic query."""
            results = client.search(query=query, limit=limit, agent_id=agent_id)
            if not results:
                return "No relevant memories found."
            return "\n".join([f"- {r.content}" for r in results])

        @tool
        def add_memory(content: str, memory_type: str = "fact") -> str:
            """Save an important detail, thought, or fact to long-term memory."""
            client.add(content=content, memory_type=memory_type, agent_id=agent_id)
            return "Memory successfully added."

        return [search_memory, add_memory]

except ImportError:
    def get_memu_tools(client: MemUClient, agent_id: Optional[str] = None) -> list[Any]:
        raise ImportError("langchain-core is not installed")
