# memu/web_search_ingest.py
from memu.models import MemoryCreate, MemoryType
from memu.temporal_client import store_memory_workflow
import json
import logging

logger = logging.getLogger(__name__)

async def ingest_web_search(query: str, results: list, agent_id: str):
    """Store web search results into memU using durable workflow."""
    
    content = f"Web Search: {query}\n\nResults:\n"
    for r in results[:5]:
        content += f"- [{r.get('title', 'No Title')}]({r.get('url', '#')}): {r.get('description', '')}\n"
    
    metadata = {
        "type": "web_search",
        "query": query,
        "results_count": len(results),
        "source": "brave"
    }
    
    wf_id = await store_memory_workflow(content, agent_id, metadata)
    if wf_id:
        logger.info(f"Queued web search ingestion: {wf_id}")
    else:
        logger.warning("Failed to queue web search ingestion")
    
    return True
