"""EvalSummary: lightweight per-subject per-dimension aggregation of
``reputation_events`` rows.

This lives in ``rumil.db`` rather than inside a specific store because it's
used both by ``AnnotationStore.get_eval_summary_for_pages`` /
``get_eval_summary_for_calls`` and by consumers like
``rumil.eval_feedback`` and
``rumil.orchestrators.policies.eval_feedback`` that don't go through
the store. Separating it avoids circular imports from ``rumil.database``.
"""

from collections.abc import Sequence
from typing import Any


class EvalSummary:
    """Lightweight numeric summary of reputation events for one subject+dimension.

    Kept as a plain class (not a pydantic model) because it's purely a
    query-time aggregate — callers consume ``mean`` / ``count`` / ``latest``
    and do not persist instances.
    """

    __slots__ = ("count", "dimension", "latest", "mean")

    def __init__(self, *, dimension: str, mean: float, count: int, latest: float) -> None:
        self.dimension = dimension
        self.mean = mean
        self.count = count
        self.latest = latest

    def __repr__(self) -> str:
        return (
            f"EvalSummary(dimension={self.dimension!r}, mean={self.mean:.3f}, "
            f"count={self.count}, latest={self.latest:.3f})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EvalSummary):
            return NotImplemented
        return (
            self.dimension == other.dimension
            and self.count == other.count
            and self.mean == other.mean
            and self.latest == other.latest
        )


def aggregate_eval_rows_by_subject(
    rows: Sequence[dict[str, Any]],
    *,
    subject_key: str,
) -> dict[str, dict[str, EvalSummary]]:
    """Group reputation_events rows by (subject_id, dimension).

    Rows must carry ``extra[subject_key]`` — otherwise they're skipped,
    not an error (the index filter already constrains to rows with
    ``extra ? 'subject_*_id'``).
    """
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        extra = r.get("extra") or {}
        subject = extra.get(subject_key)
        if not subject:
            continue
        dim = r["dimension"]
        score = float(r["score"])
        created_at = r.get("created_at") or ""
        key = (subject, dim)
        bucket = buckets.get(key)
        if bucket is None:
            buckets[key] = {
                "sum_score": score,
                "count": 1,
                "latest_score": score,
                "latest_at": created_at,
            }
        else:
            bucket["sum_score"] += score
            bucket["count"] += 1
            if created_at > bucket["latest_at"]:
                bucket["latest_at"] = created_at
                bucket["latest_score"] = score

    result: dict[str, dict[str, EvalSummary]] = {}
    for (subject, dim), b in buckets.items():
        n = b["count"]
        summary = EvalSummary(
            dimension=dim,
            mean=b["sum_score"] / n,
            count=n,
            latest=b["latest_score"],
        )
        result.setdefault(subject, {})[dim] = summary
    return result
