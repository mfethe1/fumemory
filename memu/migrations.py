import logging
from pathlib import Path
import asyncpg

logger = logging.getLogger(__name__)

async def run_migrations(pool: asyncpg.Pool):
    """Run all .sql migration files in the migrations directory."""
    migration_dir = Path(__file__).parent / "migrations"
    if not migration_dir.exists():
        logger.warning("No migrations directory found.")
        return

    async with pool.acquire() as conn:
        # Enable pgvector if available (before any migrations)
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            logger.info("pgvector extension enabled.")
        except Exception as e:
            logger.warning(f"pgvector not available (non-fatal): {e}")

        # Enable uuid-ossp (required by schema)
        try:
            await conn.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";')
            logger.info("uuid-ossp extension enabled.")
        except Exception as e:
            logger.warning(f"uuid-ossp not available: {e}")

        # Create migrations tracking table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ DEFAULT NOW(),
                success BOOLEAN DEFAULT TRUE
            );
        """)

        # Add success column if it doesn't exist (upgrade from old schema)
        await conn.execute("""
            ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS success BOOLEAN DEFAULT TRUE;
        """)

        sql_files = sorted(migration_dir.glob("*.sql"))

        for sql_file in sql_files:
            version = sql_file.name

            # Only skip if previously applied successfully
            row = await conn.fetchrow(
                "SELECT success FROM schema_migrations WHERE version = $1", version
            )
            if row and row["success"]:
                logger.debug(f"Migration {version} already applied, skipping.")
                continue

            # Remove failed/partial record so we can retry
            if row and not row["success"]:
                await conn.execute("DELETE FROM schema_migrations WHERE version = $1", version)
                logger.info(f"Retrying previously failed migration: {version}")

            logger.info(f"Applying migration: {version}")
            try:
                sql = sql_file.read_text()
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, success) VALUES ($1, TRUE) "
                    "ON CONFLICT (version) DO UPDATE SET success = TRUE, applied_at = NOW()",
                    version,
                )
                logger.info(f"Migration {version} applied successfully.")
            except Exception as e:
                logger.error(f"Migration {version} failed: {e}")
                # Record failure so we retry next boot
                try:
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, success) VALUES ($1, FALSE) "
                        "ON CONFLICT (version) DO UPDATE SET success = FALSE",
                        version,
                    )
                except Exception as record_error:
                    logger.error(
                        f"Failed to record migration failure for {version}: {record_error}"
                    )
                raise
