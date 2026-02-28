import logging
from typing import List, Dict, Optional
import json

from memu.client import MemUClient
from memu.models import MemoryCreate

logger = logging.getLogger(__name__)

async def ingest_web_search(client: MemUClient, query: str, results: List[Dict], agent_id: str):
    """
    Store web search results into memU via API (Search Vault + Memories).
    Uses the client to ensure proper embedding, audit logging, and NATS events.
    """
    
    # 1. Log the search event (Search Vault)
    try:
        # Client handles embedding computation if configured, or API does it.
        # Here we assume API does it for log_search.
        await client.log_search(
            query=query,
            summary=f"Found {len(results)} results",
            url="", # Source is aggregator
            agent_id=agent_id
        )
        logger.info(f"Logged search query in Vault: {query}")
                
    except Exception as e:
        logger.error(f"Failed to log search history: {e}")
        
    # 2. Store individual results as Memories (for Recall)
    # Filter high-quality results only
    stored_count = 0
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
        
        try:
            # Use client to add memory (triggers embedding + NATS + Audit)
            await client.add_memory(
                content=mem_content,
                agent_id=agent_id,
                memory_type="observation", # New schema compliance
                metadata=mem_meta
            )
            stored_count += 1
        except Exception as e:
            logger.warning(f"Failed to store memory result: {e}")

    logger.info(f"Ingested {stored_count} search results as memories.")
    return True
