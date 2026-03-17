"""
Migrate a workspace from local Supabase to production Supabase.

Usage:
    uv run python migrate_local_to_prod.py --workspace my-workspace
    uv run python migrate_local_to_prod.py --workspace my-workspace --dry-run
"""

import argparse
import os
import sys

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

LOCAL_URL = "http://127.0.0.1:54321"
LOCAL_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0."
    "EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"
)

CHUNK_SIZE = 100


def _rows(result) -> list[dict]:
    return result.data or []


def _chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def fetch_all(client: Client, table: str, **filters) -> list[dict]:
    """Fetch all rows from a table, paginating past the 1000-row API limit."""
    all_rows = []
    offset = 0
    while True:
        query = client.table(table).select("*").range(offset, offset + CHUNK_SIZE - 1)
        for key, value in filters.items():
            query = query.eq(key, value)
        rows = _rows(query.execute())
        all_rows.extend(rows)
        if len(rows) < CHUNK_SIZE:
            break
        offset += CHUNK_SIZE
    return all_rows


def upsert_chunks(client: Client, table: str, rows: list[dict], dry_run: bool) -> None:
    if not rows:
        return
    if dry_run:
        print(f"  [dry-run] would upsert {len(rows)} rows into {table}")
        return
    for chunk in _chunks(rows, CHUNK_SIZE):
        client.table(table).upsert(chunk).execute()
        print(f"  upserted {len(chunk)} rows into {table}")


def migrate(workspace_name: str, dry_run: bool) -> None:
    prod_url = os.environ.get("SUPABASE_PROD_URL")
    prod_key = os.environ.get("SUPABASE_PROD_KEY")
    if not prod_url or not prod_key:
        print("Error: SUPABASE_PROD_URL and SUPABASE_PROD_KEY must be set in .env")
        sys.exit(1)

    print(f"Connecting to local Supabase...")
    local = create_client(LOCAL_URL, LOCAL_KEY)

    print(f"Connecting to prod Supabase...")
    prod = create_client(prod_url, prod_key)

    # --- Find local project ---
    projects = _rows(local.table("projects").select("*").eq("name", workspace_name).execute())
    if not projects:
        print(f"Error: workspace '{workspace_name}' not found locally.")
        sys.exit(1)
    local_project = projects[0]
    local_project_id = local_project["id"]
    print(f"\nFound local project: '{workspace_name}' ({local_project_id[:8]})")

    # --- Resolve project in prod (may already exist with a different ID) ---
    print("\nMigrating project...")
    existing = _rows(prod.table("projects").select("*").eq("name", workspace_name).execute())
    if existing:
        prod_project_id = existing[0]["id"]
        print(f"  project '{workspace_name}' already exists in prod ({prod_project_id[:8]})")
    else:
        if not dry_run:
            result = _rows(prod.table("projects").insert({"name": workspace_name}).execute())
            prod_project_id = result[0]["id"]
            print(f"  created project '{workspace_name}' in prod ({prod_project_id[:8]})")
        else:
            prod_project_id = local_project_id
            print(f"  [dry-run] would create project '{workspace_name}' in prod")

    # Remap project_id on all records if prod ID differs from local ID
    id_changed = prod_project_id != local_project_id
    if id_changed:
        print(f"  remapping project_id {local_project_id[:8]} → {prod_project_id[:8]}")

    # --- Pages ---
    print("\nMigrating pages...")
    pages = fetch_all(local, "pages", project_id=local_project_id)
    print(f"  found {len(pages)} pages")
    if id_changed:
        for p in pages:
            p["project_id"] = prod_project_id
    upsert_chunks(prod, "pages", pages, dry_run)

    # --- Calls ---
    print("\nMigrating calls...")
    calls = fetch_all(local, "calls", project_id=local_project_id)
    print(f"  found {len(calls)} calls")
    if id_changed:
        for c in calls:
            c["project_id"] = prod_project_id
    # Insert calls without parent_call_id first to satisfy self-referential FK
    root_calls = [c for c in calls if not c.get("parent_call_id")]
    child_calls = [c for c in calls if c.get("parent_call_id")]
    upsert_chunks(prod, "calls", root_calls, dry_run)
    upsert_chunks(prod, "calls", child_calls, dry_run)

    # --- Collect page IDs and run IDs for downstream tables ---
    page_ids = {p["id"] for p in pages}
    call_ids = {c["id"] for c in calls}
    run_ids = {c["run_id"] for c in calls if c.get("run_id")}

    # --- Page links ---
    print("\nMigrating page_links...")
    all_links = fetch_all(local, "page_links")
    links = [l for l in all_links if l.get("from_page_id") in page_ids or l.get("to_page_id") in page_ids]
    print(f"  found {len(links)} page_links")
    upsert_chunks(prod, "page_links", links, dry_run)

    # --- Budget ---
    print("\nMigrating budget...")
    all_budget = fetch_all(local, "budget")
    budget = [b for b in all_budget if b.get("run_id") in run_ids]
    print(f"  found {len(budget)} budget rows")
    upsert_chunks(prod, "budget", budget, dry_run)

    # --- Page ratings ---
    print("\nMigrating page_ratings...")
    all_ratings = fetch_all(local, "page_ratings")
    ratings = [r for r in all_ratings if r.get("page_id") in page_ids]
    print(f"  found {len(ratings)} page_ratings")
    upsert_chunks(prod, "page_ratings", ratings, dry_run)

    # --- Page flags ---
    print("\nMigrating page_flags...")
    all_flags = fetch_all(local, "page_flags")
    flags = [f for f in all_flags if f.get("page_id") in page_ids]
    print(f"  found {len(flags)} page_flags")
    upsert_chunks(prod, "page_flags", flags, dry_run)

    # --- LLM exchanges ---
    print("\nMigrating call_llm_exchanges...")
    all_exchanges = fetch_all(local, "call_llm_exchanges")
    exchanges = [e for e in all_exchanges if e.get("call_id") in call_ids]
    print(f"  found {len(exchanges)} call_llm_exchanges")
    upsert_chunks(prod, "call_llm_exchanges", exchanges, dry_run)

    print(f"\n{'[dry-run] ' if dry_run else ''}Done. Migrated workspace '{workspace_name}' to prod.")
    if not dry_run:
        print(f"Verify with: uv run python main.py --prod --workspace {workspace_name} --list")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate a local workspace to prod Supabase")
    parser.add_argument("--workspace", required=True, help="Workspace name to migrate")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be migrated without writing")
    args = parser.parse_args()

    migrate(args.workspace, args.dry_run)
