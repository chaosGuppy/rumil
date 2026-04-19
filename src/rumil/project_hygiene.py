"""Auto-hide rules for stale scratch project workspaces.

The ``projects`` table accumulates a long tail of throwaway workspaces from
smoke tests, A/B sweeps, chat-persistence sessions, and ad-hoc
experimentation. The parma landing page hides them via the ``hidden`` flag
when the user toggles "show test projects", but that toggle still notes the
total count (e.g. "978 hidden") and the underlying rows pile up forever.

This module provides a one-shot hygiene pass: identify scratch-named
projects that have aged out without producing any claims, and flip their
``hidden`` flag to ``true``. The hide is reversible — the parma UI's
per-project unhide button still works on auto-hidden rows.

Hide criteria (all must hold):
- Name matches a scratch pattern (see ``SCRATCH_NAME_PATTERNS``).
- Zero baseline (non-staged, non-superseded) ``claim`` pages.
- Last activity (newest page or call) is older than the TTL.
- Project itself was created before the TTL cutoff (catches empty,
  never-touched workspaces).

Exposed via the ``--auto-hide-scratch`` CLI flag in ``main.py``.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from rumil.database import DB

SCRATCH_NAME_PATTERNS: Sequence[str] = (
    "chat-persist-",
    "test-",
)
SCRATCH_NAME_SUFFIXES: Sequence[str] = (
    "-scratch",
    "-smoke",
    "-test",
)

DEFAULT_MIN_AGE_DAYS = 14


def is_scratch_name(name: str) -> bool:
    """True iff ``name`` matches one of the scratch project patterns.

    Patterns are intentionally narrow to avoid sweeping up real workspaces
    like ``research`` or ``metr-distill``. Add new patterns sparingly.
    """
    return any(name.startswith(p) for p in SCRATCH_NAME_PATTERNS) or any(
        name.endswith(s) for s in SCRATCH_NAME_SUFFIXES
    )


@dataclass(frozen=True)
class AutoHideCandidate:
    project_id: str
    name: str
    created_at: datetime
    last_activity_at: datetime
    claim_count: int


async def find_auto_hide_candidates(
    db: DB,
    *,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
) -> list[AutoHideCandidate]:
    """Return scratch projects eligible for auto-hide.

    Single round trip: reuses the ``list_projects_summary`` RPC (which
    already aggregates claim_count and last_activity_at per project in
    one SQL pass) and filters in Python. Already-hidden projects are
    excluded.
    """
    cutoff = datetime.now(UTC) - timedelta(days=min_age_days)
    rows = await db.list_projects_summary(include_hidden=False)
    candidates: list[AutoHideCandidate] = []
    for row in rows:
        name: str = row["name"]
        if not is_scratch_name(name):
            continue
        if int(row["claim_count"]) > 0:
            continue
        created_at = _parse_dt(row["created_at"])
        last_activity_at = _parse_dt(row["last_activity_at"])
        if created_at >= cutoff or last_activity_at >= cutoff:
            continue
        candidates.append(
            AutoHideCandidate(
                project_id=row["project_id"],
                name=name,
                created_at=created_at,
                last_activity_at=last_activity_at,
                claim_count=int(row["claim_count"]),
            )
        )
    return candidates


async def auto_hide_scratch_projects(
    db: DB,
    *,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
    dry_run: bool = True,
) -> list[AutoHideCandidate]:
    """Find and (unless ``dry_run``) hide stale scratch projects.

    Returns the candidate list so the caller can report what changed.
    """
    candidates = await find_auto_hide_candidates(db, min_age_days=min_age_days)
    if candidates and not dry_run:
        await db.bulk_hide_projects([c.project_id for c in candidates])
    return candidates


def _parse_dt(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)
