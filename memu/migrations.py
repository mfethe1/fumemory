import logging
import os
from pathlib import Path
import asyncpg

logger = logging.getLogger(__name__)

async def run_migrations(pool: asyncpg.Pool):
    """Run all .sql migration files in the migrations directory."""
    migration_dir = Path(__file__).parent / "migrations"
    if not migration_dir.exists():
        logger.warning("No migrations directory found.")
        return

    # Enable pgvector if available
    async with pool.acquire() as conn:
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            logger.info("pgvector extension enabled.")
        except Exception as e:
            logger.warning(f"pgvector not available (non-fatal): {e}")

        # Create migrations table if not exists
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        
        # Get sorted list of sql files
        sql_files = sorted(migration_dir.glob("*.sql"))
        
        for sql_file in sql_files:
            version = sql_file.name
            
            # Check if applied
            row = await conn.fetchrow("SELECT 1 FROM schema_migrations WHERE version = $1", version)
            if row:
                continue
                
            logger.info(f"Applying migration: {version}")
            try:
                sql = sql_file.read_text()
                await conn.execute(sql)
                await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", version)
                logger.info(f"Migration {version} applied successfully.")
            except Exception as e:
                logger.error(f"Migration {version} failed: {e}")
                logger.warning(f"Migration startup failed: {e}")
                # Continue to next migration instead of breaking startup
