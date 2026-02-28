import asyncio
import os
import asyncpg

# Load from secrets or env
try:
    with open(os.path.expanduser("~/.openclaw/secrets/railway_memu_db_url"), "r") as f:
        DATABASE_URL = f.read().strip()
except Exception:
    DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    print("❌ Missing DATABASE_URL")
    exit(1)

MIGRATION_SQL = """
-- Migration: Add audit_log and search_history tables
CREATE TABLE IF NOT EXISTS audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_type TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS search_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    results_count INTEGER DEFAULT 0,
    search_type TEXT DEFAULT 'text', -- 'text' or 'vector'
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_log(agent_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action_type);
CREATE INDEX IF NOT EXISTS idx_search_query ON search_history USING gin(to_tsvector('english', query));
"""

async def apply():
    print(f"Connecting to {DATABASE_URL.split('@')[1]}...")
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(MIGRATION_SQL)
        print("✅ Migration 003_audit_search applied successfully.")
    except Exception as e:
        print(f"❌ Migration failed: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(apply())
