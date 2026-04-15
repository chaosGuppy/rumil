"""List root questions in the active (or explicitly specified) rumil workspace.

Usage (run via `uv run python -m rumil_skills.list_questions`; the SKILL.md
handles PYTHONPATH so the package is importable):

    # Default workspace from session state
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.list_questions

    # Explicit workspace
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.list_questions --workspace my-ws
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ._format import truncate
from ._runctx import make_db


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=None)
    args = parser.parse_args()

    db, ws = await make_db(workspace=args.workspace)
    try:
        questions = await db.get_root_questions()
    finally:
        await db.close()

    print(f"workspace: {ws}")
    if not questions:
        print("(no root questions)")
        return

    print(f"{len(questions)} root question(s):")
    for q in questions:
        created = q.created_at.strftime("%Y-%m-%d")
        print(f"  {q.id[:8]}  {created}  {truncate(q.headline, 70)}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
