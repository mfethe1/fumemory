import asyncio
import os
import asyncpg

async def main():
    url = os.environ.get("DATABASE_URL") # This is pointing to /memu
    if not url:
        return
    # Connect to the default 'postgres' database
    base_url = url.rsplit('/', 1)[0] + '/postgres'
    print(f"Connecting to {base_url}")
    conn = await asyncpg.connect(base_url)
    try:
        await conn.execute("CREATE DATABASE temporal;")
        print("Database 'temporal' created.")
    except asyncpg.exceptions.DuplicateDatabaseError:
        print("Database 'temporal' already exists.")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
