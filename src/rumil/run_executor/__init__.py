"""RunExecutor: unified control plane for rumil runs.

Phase 2 of the Run-as-control-plane refactor. Today this package
exposes only the data model + a read-only ``status()`` query. The
active coordination paths (``main.py``'s six cmd_* scaffolds,
``scripts/run_call.py``, and ``api/app.py``'s _run_background family)
are NOT yet migrated — they keep doing their own scaffolding (create
run_id, init_budget, create_run, dispatch orchestrator). Subsequent
phases add ``start()`` (writing ``runs.status = 'running'`` +
``started_at``), ``cancel()`` / ``pause()`` / ``resume()``,
concurrency caps, and crash-resilient resume over
``run_checkpoints``.

The exported surface is deliberately small and stable so callers
(frontend, parma, chat skills) can build against it before the
imperative path is retired.
"""

from rumil.run_executor.executor import RunExecutor
from rumil.run_executor.run_spec import RunSpec
from rumil.run_executor.run_state import RunStatus, RunView

__all__ = [
    "RunExecutor",
    "RunSpec",
    "RunStatus",
    "RunView",
]
