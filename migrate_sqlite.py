"""
Migrate data from the old SQLite database to the new local Supabase instance.

Usage:
    uv run python migrate_sqlite.py
    uv run python migrate_sqlite.py --db-path path/to/workspace.db
    uv run python migrate_sqlite.py --run-id my_legacy_data
"""

import argparse
import json
import sqlite3
import sys

from supabase import create_client

SUPABASE_URL = "http://127.0.0.1:54321"
SUPABASE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0."
    "EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"
)

DEFAULT_DB_PATH = "db/workspace.db"
DEFAULT_RUN_ID = "__legacy__"

TABLES = ["pages", "page_links", "calls", "page_ratings", "page_flags"]


def get_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cursor.fetchall()]


def migrate(db_path: str, run_id: str) -> None:
    print(f"Connecting to SQLite: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    print(f"Connecting to Supabase: {SUPABASE_URL}")
    client = create_client(SUPABASE_URL, SUPABASE_KEY)

    # --- Budget ---
    print("\nMigrating budget...")
    try:
        row = conn.execute("SELECT total, used FROM budget WHERE id = 1").fetchone()
        if row:
            client.table("budget").upsert({
                "run_id": run_id,
                "total": row["total"],
                "used": row["used"],
            }).execute()
            print(f"  budget: total={row['total']}, used={row['used']}")
        else:
            print("  budget: no row found, skipping")
    except sqlite3.OperationalError as e:
        print(f"  budget: skipped ({e})")

    # --- Main tables ---
    for table in TABLES:
        try:
            columns = get_columns(conn, table)
        except sqlite3.OperationalError:
            print(f"\n{table}: table not found in SQLite, skipping")
            continue

        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            print(f"\n{table}: empty, skipping")
            continue

        print(f"\nMigrating {table} ({len(rows)} rows)...")
        batch = []
        for row in rows:
            record = dict(row)
            # Parse JSON columns stored as strings in SQLite
            for col in ("extra", "context_page_ids", "review_json"):
                if col in record and isinstance(record[col], str):
                    try:
                        record[col] = json.loads(record[col])
                    except (json.JSONDecodeError, TypeError):
                        pass
            record["run_id"] = run_id
            # calls.trace_json doesn't exist in old SQLite — default to empty list
            if table == "calls" and "trace_json" not in record:
                record["trace_json"] = []
            batch.append(record)

        # Insert in chunks to avoid payload size limits
        chunk_size = 100
        for i in range(0, len(batch), chunk_size):
            chunk = batch[i:i + chunk_size]
            client.table(table).upsert(chunk).execute()
            print(f"  inserted rows {i + 1}–{min(i + chunk_size, len(batch))}")

    conn.close()
    print(f"\nDone. All data migrated with run_id='{run_id}'.")
    print("Run `uv run python main.py --list` to verify.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate SQLite workspace to Supabase")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="Path to SQLite .db file")
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID, help="run_id to assign migrated data")
    args = parser.parse_args()

    migrate(args.db_path, args.run_id)
