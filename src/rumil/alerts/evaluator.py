"""Compute-on-read evaluator for alert rules.

Each alert rule has:
  * built-in default ``params`` in ``DEFAULT_RULES``
  * an ``evaluate_<kind>`` coroutine that reads DB state and returns
    a list of ``FiredAlert`` rows (empty if nothing is firing)

``evaluate_alerts(db, run_id)`` is the top-level entry point: it
resolves the effective config per kind (run-specific → project-wide →
default) and dispatches to the kind's evaluator.

No persistence — callers decide what to do with the returned list.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from rumil.database import DB
from rumil.db.row_helpers import _rows
from rumil.models import AlertConfig, AlertKind, AlertSeverity, FiredAlert

DEFAULT_RULES: dict[AlertKind, dict[str, Any]] = {
    AlertKind.COST_THRESHOLD: {"pct_of_cap": 0.8, "absolute_usd": None},
    AlertKind.STALL_TIMEOUT: {"minutes": 15},
    AlertKind.CONFUSION_SPIKE: {"window_min": 30, "threshold": 2.0, "min_count": 2},
}


def resolve_config_for_kind(
    kind: AlertKind,
    configs: Sequence[AlertConfig],
    run_id: str,
    project_id: str | None,
) -> tuple[dict[str, Any], str | None]:
    """Return ``(params, source_config_id)`` for this kind on this run.

    Most-specific-wins: run-scoped config → project-scoped → defaults.
    Disabled configs short-circuit: if a run-scoped config has
    ``enabled=False``, the kind is muted entirely for that run.
    """
    run_match = next(
        (c for c in configs if c.kind == kind and c.run_id == run_id),
        None,
    )
    if run_match is not None:
        if not run_match.enabled:
            return {}, None
        params = {**DEFAULT_RULES[kind], **(run_match.params or {})}
        return params, run_match.id

    if project_id:
        project_match = next(
            (
                c
                for c in configs
                if c.kind == kind and c.run_id is None and c.project_id == project_id
            ),
            None,
        )
        if project_match is not None:
            if not project_match.enabled:
                return {}, None
            params = {**DEFAULT_RULES[kind], **(project_match.params or {})}
            return params, project_match.id

    return dict(DEFAULT_RULES[kind]), None


async def _evaluate_cost_threshold(
    db: DB, run_id: str, params: dict[str, Any], source_id: str | None
) -> list[FiredAlert]:
    cost_rows = _rows(
        await db._execute(db.client.table("call_costs").select("usd").eq("run_id", run_id))
    )
    total_usd = sum((Decimal(str(r.get("usd") or 0)) for r in cost_rows), Decimal(0))
    run_row = _rows(
        await db._execute(db.client.table("runs").select("cost_usd_cents, config").eq("id", run_id))
    )
    cap_cents = None
    if run_row:
        cfg = run_row[0].get("config") or {}
        cap_cents = cfg.get("cost_cap_cents")

    absolute_usd = params.get("absolute_usd")
    if absolute_usd is not None and total_usd >= Decimal(str(absolute_usd)):
        return [
            FiredAlert(
                run_id=run_id,
                kind=AlertKind.COST_THRESHOLD,
                severity=AlertSeverity.WARN,
                message=f"Spend ${total_usd:.2f} crossed absolute cap ${absolute_usd}",
                context={"usd": float(total_usd), "absolute_usd": float(absolute_usd)},
                source_config_id=source_id,
            )
        ]

    if cap_cents:
        cap_usd = Decimal(cap_cents) / Decimal(100)
        pct = params.get("pct_of_cap", 0.8)
        threshold_usd = cap_usd * Decimal(str(pct))
        if total_usd >= threshold_usd:
            severity = AlertSeverity.CRIT if total_usd >= cap_usd else AlertSeverity.WARN
            return [
                FiredAlert(
                    run_id=run_id,
                    kind=AlertKind.COST_THRESHOLD,
                    severity=severity,
                    message=(
                        f"Spend ${total_usd:.2f} is {float(total_usd / cap_usd) * 100:.0f}% "
                        f"of cap ${cap_usd:.2f}"
                    ),
                    context={
                        "usd": float(total_usd),
                        "cap_usd": float(cap_usd),
                        "pct_of_cap": float(pct),
                    },
                    source_config_id=source_id,
                )
            ]
    return []


async def _evaluate_stall_timeout(
    db: DB, run_id: str, params: dict[str, Any], source_id: str | None
) -> list[FiredAlert]:
    minutes = int(params.get("minutes", 15))
    rows = _rows(
        await db._execute(
            db.client.table("calls")
            .select("completed_at, status")
            .eq("run_id", run_id)
            .order("completed_at", desc=True)
            .limit(1)
        )
    )
    run_row = _rows(
        await db._execute(db.client.table("runs").select("status, started_at").eq("id", run_id))
    )
    if not run_row:
        return []
    if run_row[0].get("status") not in ("running", "pending"):
        return []

    last_activity: datetime | None = None
    if rows and rows[0].get("completed_at"):
        last_activity = datetime.fromisoformat(rows[0]["completed_at"])
    elif run_row[0].get("started_at"):
        last_activity = datetime.fromisoformat(run_row[0]["started_at"])
    if last_activity is None:
        return []

    now = datetime.now(UTC)
    idle = now - last_activity
    if idle < timedelta(minutes=minutes):
        return []

    return [
        FiredAlert(
            run_id=run_id,
            kind=AlertKind.STALL_TIMEOUT,
            severity=AlertSeverity.WARN,
            message=f"No new call completion in {int(idle.total_seconds() / 60)} minutes",
            context={"minutes_idle": int(idle.total_seconds() / 60), "threshold": minutes},
            source_config_id=source_id,
        )
    ]


async def _evaluate_confusion_spike(
    db: DB, run_id: str, params: dict[str, Any], source_id: str | None
) -> list[FiredAlert]:
    window_min = int(params.get("window_min", 30))
    threshold = float(params.get("threshold", 2.0))
    min_count = int(params.get("min_count", 2))
    cutoff = (datetime.now(UTC) - timedelta(minutes=window_min)).isoformat()

    rows = _rows(
        await db._execute(
            db.client.table("reputation_events")
            .select("score, created_at")
            .eq("run_id", run_id)
            .eq("source", "confusion_scan")
            .eq("dimension", "confusion")
            .gte("created_at", cutoff)
        )
    )
    if len(rows) < min_count:
        return []
    avg_score = sum(float(r.get("score") or 0) for r in rows) / len(rows)
    if avg_score < threshold:
        return []

    return [
        FiredAlert(
            run_id=run_id,
            kind=AlertKind.CONFUSION_SPIKE,
            severity=AlertSeverity.WARN,
            message=(
                f"{len(rows)} confusion signals in last {window_min}min "
                f"(avg score {avg_score:.2f}, threshold {threshold:.2f})"
            ),
            context={
                "count": len(rows),
                "avg_score": avg_score,
                "threshold": threshold,
                "window_min": window_min,
            },
            source_config_id=source_id,
        )
    ]


_EVALUATORS = {
    AlertKind.COST_THRESHOLD: _evaluate_cost_threshold,
    AlertKind.STALL_TIMEOUT: _evaluate_stall_timeout,
    AlertKind.CONFUSION_SPIKE: _evaluate_confusion_spike,
}


async def evaluate_alerts(
    db: DB,
    run_id: str,
    project_id: str | None = None,
) -> list[FiredAlert]:
    """Compute currently-firing alerts for ``run_id``.

    Reads run-scoped + project-scoped configs from ``alert_configs`` and
    layers them over ``DEFAULT_RULES`` per kind. Calls each kind's
    evaluator and concatenates the fired-list.
    """
    configs = await db.alert_configs.list_for_run(run_id, project_id=project_id)
    fired: list[FiredAlert] = []
    for kind, evaluator in _EVALUATORS.items():
        params, source_id = resolve_config_for_kind(kind, configs, run_id, project_id)
        if not params:
            continue
        fired.extend(await evaluator(db, run_id, params, source_id))
    return fired
