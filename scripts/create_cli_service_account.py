"""One-shot script to create the CLI service-account user in Supabase Auth.

The CLI mints a short-lived JWT signed with `SUPABASE_JWT_SECRET` whose
`sub` claim is the user_id of this service account. By baking the UUID
into `Settings.default_cli_user_id` (committed to the repo), every CLI
operator can submit `--executor prod` jobs without per-person auth setup;
individuals who want to authenticate as themselves can still override via
the `DEFAULT_CLI_USER_ID` env var.

Run once against your target Supabase, paste the printed UUID into
`src/rumil/settings.py` as the default for `default_cli_user_id`.

    uv run python scripts/create_cli_service_account.py            # local
    uv run python scripts/create_cli_service_account.py --prod     # prod
"""

import argparse
import asyncio
import secrets
import sys

from rumil.settings import get_settings
from supabase import AsyncClientOptions, acreate_client

CLI_SERVICE_EMAIL = "cli-service@rumil.local"


async def _find_user_id_by_email(client, email: str) -> str | None:
    """Search auth.admin.list_users for an existing user with this email."""
    page = 1
    while True:
        users = await client.auth.admin.list_users(page=page, per_page=200)
        if not users:
            return None
        for u in users:
            if (getattr(u, "email", "") or "").lower() == email.lower():
                return str(u.id)
        if len(users) < 200:
            return None
        page += 1


async def _create_or_fetch(prod: bool) -> str:
    settings = get_settings()
    url, key = settings.get_supabase_credentials(prod)
    client = await acreate_client(url, key, options=AsyncClientOptions(schema="public"))
    try:
        existing = await _find_user_id_by_email(client, CLI_SERVICE_EMAIL)
        if existing:
            print(f"existing service account found: {existing}", file=sys.stderr)
            return existing

        random_password = secrets.token_urlsafe(32)
        created = await client.auth.admin.create_user(
            {
                "email": CLI_SERVICE_EMAIL,
                "password": random_password,
                "email_confirm": True,
                "user_metadata": {"purpose": "rumil-cli-service-account"},
            }
        )
        new_user = getattr(created, "user", None) or created
        new_id = str(getattr(new_user, "id", "") or "")
        if not new_id:
            raise RuntimeError(f"create_user returned no id: {created!r}")
        print(f"created service account: {new_id}", file=sys.stderr)
        return new_id
    finally:
        # AsyncClient has no public close; the underlying httpx client is gc'd.
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prod",
        action="store_true",
        help="Target prod Supabase (requires SUPABASE_PROD_URL/SUPABASE_PROD_KEY).",
    )
    args = parser.parse_args()

    user_id = asyncio.run(_create_or_fetch(prod=args.prod))
    # UUID goes to stdout (machine-readable), informational lines to stderr.
    print(user_id)
    print(
        "\nNext step: paste this UUID as the default for "
        "`default_cli_user_id` in src/rumil/settings.py and commit.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
