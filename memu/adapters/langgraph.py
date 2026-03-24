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

        @tool
        def core_mem_append(text: str) -> str:
            """Append text to your Working_Context core memory block.
            Use this to save important context you want to persist across turns."""
            resp = client._client.post(
                f"/api/v1/memu/core-memory/{agent_id}/Working_Context/append",
                json={"text": text},
            )
            resp.raise_for_status()
            data = resp.json()
            return f"Appended to Working_Context ({data.get('tokens', '?')} tokens total)"

        @tool
        def core_mem_replace(old_text: str, new_text: str) -> str:
            """Replace specific text in your Working_Context core memory block.
            Use this to update or correct information in your working context."""
            resp = client._client.post(
                f"/api/v1/memu/core-memory/{agent_id}/Working_Context/replace",
                json={"old_text": old_text, "new_text": new_text},
            )
            resp.raise_for_status()
            return "Replaced text in Working_Context"

        @tool
        def page_archival(query: str, limit: int = 3) -> str:
            """Search archival (long-term) memory and page results into your working context.
            Use when you need to recall information from past sessions."""
            resp = client._client.post(
                f"/api/v1/memu/core-memory/{agent_id}/page-archival",
                json={"query": query, "limit": limit},
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("paged", [])
            if not results:
                return "No archival memories matched your query."
            return "\n".join([f"- {r['content']}" for r in results])

        return [search_memory, add_memory, core_mem_append, core_mem_replace, page_archival]

except ImportError:
    def get_memu_tools(client: MemUClient, agent_id: Optional[str] = None) -> list[Any]:
        raise ImportError("langchain-core is not installed")
