"""Grant, revoke, or list admins for the rumil web UI.

Admin status is stored in the `user_admins` table; presence in that table
unlocks the trace viewer, statistics pages, and ab-eval pages. This script
is the supported way to manage that table — looks up the auth.users row by
email and writes via the service-role-keyed DB client.

    uv run python scripts/grant_admin.py --email x@y.z
    uv run python scripts/grant_admin.py --email x@y.z --prod
    uv run python scripts/grant_admin.py --email x@y.z --revoke
    uv run python scripts/grant_admin.py --list --prod
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from rumil.database import DB


async def _do_grant(db: DB, email: str, note: str | None) -> int:
    user_id = await db.find_auth_user_id_by_email(email)
    if not user_id:
        print(f"No auth user found with email {email!r}", file=sys.stderr)
        return 1
    await db.grant_admin(user_id, note=note)
    print(f"Granted admin to {email} ({user_id})")
    return 0


async def _do_revoke(db: DB, email: str) -> int:
    user_id = await db.find_auth_user_id_by_email(email)
    if not user_id:
        print(f"No auth user found with email {email!r}", file=sys.stderr)
        return 1
    await db.revoke_admin(user_id)
    print(f"Revoked admin from {email} ({user_id})")
    return 0


async def _do_list(db: DB) -> int:
    rows = await db.list_admin_users()
    if not rows:
        print("(no admins)")
        return 0
    for r in rows:
        email = r.get("email") or "(unknown)"
        note = f"  {r['note']}" if r.get("note") else ""
        print(f"{r['user_id']}  {email}  granted_at={r['granted_at']}{note}")
    return 0


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="user's email in Supabase Auth")
    parser.add_argument("--revoke", action="store_true", help="remove admin")
    parser.add_argument("--list", action="store_true", help="list current admins")
    parser.add_argument("--note", help="optional note attached to the grant")
    parser.add_argument("--prod", action="store_true", help="target the prod database")
    args = parser.parse_args()

    if not args.list and not args.email:
        parser.error("either --email or --list is required")

    db = await DB.create(run_id=str(uuid.uuid4()), prod=args.prod)
    try:
        if args.list:
            return await _do_list(db)
        if args.revoke:
            return await _do_revoke(db, args.email)
        return await _do_grant(db, args.email, args.note)
    finally:
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
