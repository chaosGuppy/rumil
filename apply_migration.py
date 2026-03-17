"""
Apply a raw SQL migration to prod Supabase using the service role key.

Usage:
    uv run python apply_migration.py
"""

import asyncio
import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SQL = """
ALTER TABLE pages ADD COLUMN IF NOT EXISTS summary_short TEXT NOT NULL DEFAULT '';
ALTER TABLE pages ADD COLUMN IF NOT EXISTS summary_medium TEXT NOT NULL DEFAULT '';
"""

async def main():
    url = os.environ["SUPABASE_PROD_URL"]
    key = os.environ["SUPABASE_PROD_KEY"]
    client = create_client(url, key)
    result = client.rpc("exec_sql", {"sql": SQL}).execute()
    print("Done:", result)

if __name__ == "__main__":
    asyncio.run(main())
