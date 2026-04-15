"""Local-DB guard for skill scripts.

By default every rumil-* skill refuses to run against production. Override
with RUMIL_ALLOW_PROD=1 and pass --prod explicitly. This is belt-and-suspenders
protection for an agentic caller: even if Claude decides to pass --prod, the
env var gate means the user had to flip it in their shell first.
"""

import os
import sys


def assert_local_ok(prod: bool) -> None:
    """Abort unless the run target is local, or prod has been explicitly allowed."""
    if not prod:
        return
    if os.environ.get("RUMIL_ALLOW_PROD") == "1":
        return
    print(
        "rumil-skill safety: --prod is blocked unless RUMIL_ALLOW_PROD=1 is set in "
        "the shell. The skills default to local Supabase. Refusing to continue.",
        file=sys.stderr,
    )
    sys.exit(2)
