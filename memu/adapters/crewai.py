from typing import Optional, Any

try:
    from crewai.tools import BaseTool
    from pydantic import BaseModel, Field
    
    class MemUSearchInput(BaseModel):
        query: str = Field(..., description="The query to search the memory for.")
        limit: int = Field(5, description="Maximum number of results to return.")

    class MemUAddInput(BaseModel):
        content: str = Field(..., description="The memory content to store.")
        memory_type: str = Field("fact", description="The type of memory to store, e.g., 'fact', 'thought'.")

    class MemUSearchTool(BaseTool):
        name: str = "Search Memory"
        description: str = "Search previous agent memory or knowledge base using semantic search."
        args_schema: Any = MemUSearchInput
        client: Any = None
        agent_id: Optional[str] = None
        
        def _run(self, query: str, limit: int = 5) -> str:
            results = self.client.search(query=query, limit=limit, agent_id=self.agent_id)
            if not results:
                return "No relevant memories found."
            return "\n".join([f"- {r.content}" for r in results])

    class MemUAddTool(BaseTool):
        name: str = "Add Memory"
        description: str = "Save an important fact, detail, or thought to long-term memory."
        args_schema: Any = MemUAddInput
        client: Any = None
        agent_id: Optional[str] = None
        
        def _run(self, content: str, memory_type: str = "fact") -> str:
            self.client.add(content=content, memory_type=memory_type, agent_id=self.agent_id)
            return "Memory successfully added."

except ImportError:
    MemUSearchTool = None
    MemUAddTool = None
