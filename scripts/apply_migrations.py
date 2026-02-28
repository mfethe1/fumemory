import os
import asyncio
import asyncpg
import glob
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def apply_migrations():
    db_url = os.environ.get("DATABASE_URL", "postgresql://memu:memu@localhost:5432/memu")
    
    conn = await asyncpg.connect(db_url)
    try:
        # Create migrations table if not exists
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        
        # Get applied migrations
        applied = set()
        try:
            rows = await conn.fetch("SELECT version FROM schema_migrations")
            for r in rows:
                applied.add(r['version'])
        except Exception:
            pass # Maybe table empty
            
        # Scan files
        migration_dir = os.path.join(os.path.dirname(__file__), "../memu/migrations")
        files = sorted(glob.glob(os.path.join(migration_dir, "*.sql")))
        
        for fpath in files:
            fname = os.path.basename(fpath)
            version = fname.split('_')[0]
            
            if version in applied:
                continue
                
            logger.info(f"Applying migration: {fname}")
            with open(fpath, 'r') as f:
                sql = f.read()
                
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", version)
                
            logger.info(f"✅ Applied {fname}")
            
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(apply_migrations())
