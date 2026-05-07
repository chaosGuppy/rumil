"""Grant, revoke, or list admins for the rumil web UI.

Admin status is stored in the `user_admins` table; presence in that table
unlocks the trace viewer, statistics pages, and ab-eval pages. This script
is the supported way to manage that table — looks up the auth.users row by
email and writes via the service-role-keyed DB client.

    uv run python scripts/grant_admin.py --email x@y.z
    uv run python scripts/grant_admin.py --email x@y.z --prod
    uv run python scripts/grant_admin.py --email x@y.z --revoke
    uv run python scripts/grant_admin.py --list --prod
    uv run python scripts/grant_admin.py --sync-gcp --gcp-project my-proj --prod
"""

import argparse
import asyncio
import json
import subprocess
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


def _gcp_user_emails(project: str) -> list[str]:
    """Return unique `user:` member emails from the GCP project IAM policy.

    Service accounts (`serviceAccount:`), groups, and domains are excluded.
    """
    result = subprocess.run(
        ["gcloud", "projects", "get-iam-policy", project, "--format=json"],
        capture_output=True,
        text=True,
        check=True,
    )
    policy = json.loads(result.stdout)
    emails: set[str] = set()
    for binding in policy.get("bindings", []):
        for member in binding.get("members", []):
            if member.startswith("user:"):
                emails.add(member[len("user:") :].strip().lower())
    return sorted(emails)


async def _do_sync_gcp(db: DB, project: str, note: str | None, dry_run: bool) -> int:
    emails = _gcp_user_emails(project)
    if not emails:
        print(f"No user: principals found in GCP project {project!r}", file=sys.stderr)
        return 1
    print(f"Found {len(emails)} non-service-account principal(s) in {project}:")
    for e in emails:
        print(f"  - {e}")
    if dry_run:
        print("(dry-run — no changes made)")
        return 0
    granted = 0
    missing: list[str] = []
    for email in emails:
        user_id = await db.find_auth_user_id_by_email(email)
        if not user_id:
            missing.append(email)
            continue
        await db.grant_admin(user_id, note=note)
        print(f"Granted admin to {email} ({user_id})")
        granted += 1
    print(f"\nGranted admin to {granted}/{len(emails)} user(s).")
    if missing:
        print(f"Skipped {len(missing)} (no matching Supabase auth user):", file=sys.stderr)
        for email in missing:
            print(f"  - {email}", file=sys.stderr)
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
    parser.add_argument(
        "--sync-gcp",
        action="store_true",
        help="grant admin to all user: principals in --gcp-project's IAM policy",
    )
    parser.add_argument("--gcp-project", help="GCP project id to read IAM policy from")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="with --sync-gcp, list candidate users without granting",
    )
    parser.add_argument("--note", help="optional note attached to the grant")
    parser.add_argument("--prod", action="store_true", help="target the prod database")
    args = parser.parse_args()

    if not args.list and not args.email and not args.sync_gcp:
        parser.error("one of --email, --list, or --sync-gcp is required")
    if args.sync_gcp and not args.gcp_project:
        parser.error("--sync-gcp requires --gcp-project")

    db = await DB.create(run_id=str(uuid.uuid4()), prod=args.prod)
    try:
        if args.list:
            return await _do_list(db)
        if args.sync_gcp:
            return await _do_sync_gcp(db, args.gcp_project, args.note, args.dry_run)
        if args.revoke:
            return await _do_revoke(db, args.email)
        return await _do_grant(db, args.email, args.note)
    finally:
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
