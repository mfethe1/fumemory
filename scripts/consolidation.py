import os
import asyncio
import asyncpg
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def run_consolidation():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL missing")
        return
        
    conn = await asyncpg.connect(db_url)
    try:
        # 1. Deduplicate entries
        # Keep the one with highest confidence or most recently updated
        logger.info("Deduplicating entries...")
        await conn.execute("""
            WITH duplicates AS (
                SELECT id,
                       ROW_NUMBER() OVER(PARTITION BY content_hash ORDER BY confidence DESC, updated_at DESC) as rnum
                FROM memories
                WHERE content_hash IS NOT NULL
            )
            DELETE FROM memories
            WHERE id IN (SELECT id FROM duplicates WHERE rnum > 1);
        """)

        # 2. Merge conflicting facts
        logger.info("Merging conflicting facts...")
        # Since merging text is complex, we will just keep the most confident one and downrank others
        # if they have high similarity but different conclusions (for now we just downrank older facts)

        # 3. Down-rank stale items
        logger.info("Down-ranking stale items...")
        await conn.execute("""
            UPDATE memories
            SET decay_score = decay_score * 0.9,
                confidence = confidence * 0.95
            WHERE updated_at < NOW() - INTERVAL '30 days'
              AND memory_type = 'fact';
        """)
        
        logger.info("Consolidation complete.")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(run_consolidation())