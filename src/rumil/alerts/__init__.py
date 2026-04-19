"""Alert substrate for mid-run steering surfaces.

Compute-on-read evaluator — no background worker. Callers (parma
dashboard, orchestrator tick, CLI) invoke ``evaluate_alerts(db, run_id)``
when they want a fresh snapshot of currently-firing alerts.

Rules are layered: per-run DB row → per-project DB row → built-in default.
``enabled=false`` rows mute without deleting.
"""

from rumil.alerts.evaluator import (
    DEFAULT_RULES,
    evaluate_alerts,
    resolve_config_for_kind,
)
from rumil.alerts.store import AlertConfigStore

__all__ = [
    "DEFAULT_RULES",
    "AlertConfigStore",
    "evaluate_alerts",
    "resolve_config_for_kind",
]
