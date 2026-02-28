from memu.models import MemoryCreate
import json
import logging

logger = logging.getLogger(__name__)

async def ingest_web_search(pool, query: str, results: list, agent_id: str):
    """Store web search results into memU for future retrieval."""
    
    # 1. Store the search event itself
    content = f"Web Search: {query}"
    metadata = {
        "type": "web_search",
        "results_count": len(results),
        "source": "brave", # or user's search provider
        "timestamp": "NOW()"
    }
    
    async with pool.acquire() as conn:
        # Check if identical search exists recently? For now, just append.
        
        # Store individual results as memories if valuable
        for res in results:
            title = res.get("title", "No Title")
            snippet = res.get("description", "")
            url = res.get("url", "")
            
            if not snippet: continue
            
            mem_content = f"[{title}]({url}): {snippet}"
            mem_meta = {
                "source": "web_search_result",
                "query": query,
                "url": url
            }
            
            # Use upsert logic via existing API flow or direct DB
            # For speed, direct insert ignoring dupes (or rely on content_hash dedup in schema)
            try:
                await conn.execute(
                    """
                    INSERT INTO memories (content, agent_id, memory_type, metadata)
                    VALUES ($1, $2, 'external', $3)
                    ON CONFLICT DO NOTHING
                    """,
                    mem_content, agent_id, json.dumps(mem_meta)
                )
            except Exception as e:
                logger.warning(f"Failed to store search result: {e}")
                
    return True
