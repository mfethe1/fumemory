import logging
from datetime import datetime
from typing import List, Dict, Optional
import json

from memu.models import get_embedding, MemoryCreate
from memu.client import MemUClient

logger = logging.getLogger(__name__)

async def ingest_web_search(pool, query: str, results: List[Dict], agent_id: str):
    """
    Store web search results into memU (vector Search Vault) and memories.
    """
    
    # 1. Compute embedding for the query
    try:
        embedding = await get_embedding(query)
    except Exception as e:
        logger.error(f"Failed to compute embedding for search query: {e}")
        embedding = None
        
    # 2. Insert into search_history (Search Vault)
    # Using asyncpg execute
    search_id = None
    try:
        async with pool.acquire() as conn:
            # Check if this exact search query exists recently? (Optional dedup logic)
            # For now, just insert.
            
            row = await conn.fetchrow(
                """
                INSERT INTO search_history (query, agent_id, results_count, search_type, metadata, embedding)
                VALUES ($1, $2, $3, 'vector', $4, $5)
                RETURNING id
                """,
                query,
                agent_id,
                len(results),
                json.dumps({"provider": "brave", "timestamp": str(datetime.now())}),
                embedding
            )
            if row:
                search_id = row['id']
                logger.info(f"Stored search query in Vault: {search_id}")
                
    except Exception as e:
        logger.error(f"Failed to store search history: {e}")
        
    # 3. Store individual results as Memories (for Recall)
    # Keep Macklemore's logic but ensure it's safe
    async with pool.acquire() as conn:
        for res in results:
            title = res.get("title", "No Title")
            snippet = res.get("description", "")
            url = res.get("url", "")
            
            if not snippet: continue
            
            mem_content = f"[{title}]({url}): {snippet}"
            mem_meta = {
                "source": "web_search_result",
                "query": query,
                "url": url,
                "search_id": str(search_id) if search_id else None
            }
            
            try:
                # Use raw SQL for speed or call internal API
                await conn.execute(
                    """
                    INSERT INTO memories (content, agent_id, memory_type, metadata)
                    VALUES ($1, $2, 'external', $3)
                    ON CONFLICT DO NOTHING
                    """,
                    mem_content, agent_id, json.dumps(mem_meta)
                )
            except Exception as e:
                logger.warning(f"Failed to store memory result: {e}")

    return True
