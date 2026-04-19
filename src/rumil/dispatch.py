"""Shared dispatch functions.

Every surface (CLI, API, chat, skills) that runs an orchestrator,
evaluation, or grounding/feedback pipeline routes through one of these
functions. No per-surface re-implementations.

The dispatch functions assume the caller has already:
  - created a DB instance with the desired ``run_id``
  - initialized budget (where relevant)
  - created a ``runs`` record (for traces to appear cleanly)

They do *not* create background tasks — that's a caller concern. Progress
callbacks are supported but optional; the pipeline runners don't stream
internally, so progress messages are coarse-grained ("starting", "done").
"""

from collections.abc import Callable
from typing import Any

from rumil.database import DB
from rumil.evaluate.registry import (
    get_evaluation_type_spec,
    get_grounding_pipeline_spec,
)
from rumil.evaluate.runner import run_evaluation
from rumil.models import Call
from rumil.orchestrators.registry import build_orchestrator, get_orchestrator_spec
from rumil.settings import get_settings, override_settings
from rumil.tracing.broadcast import Broadcaster


def _collect_overrides(**kwargs: object) -> dict[str, object]:
    return {k: v for k, v in kwargs.items() if v is not None}


async def dispatch_orchestrator(
    question_id: str,
    db: DB,
    *,
    variant: str | None = None,
    available_calls: str | None = None,
    available_moves: str | None = None,
    enable_global_prio: bool | None = None,
    broadcaster: Broadcaster | None = None,
    on_progress: Callable[[str], Any] | None = None,
) -> None:
    """Run an orchestrator against *question_id*.

    Any non-None override is applied via ``override_settings`` for the
    duration of the run. The orchestrator itself is built from the
    registry; variant resolution also respects the settings override.

    Caller is responsible for creating the DB, seeding budget, and
    creating the ``runs`` row.
    """
    overrides = _collect_overrides(
        prioritizer_variant=variant,
        available_calls=available_calls,
        available_moves=available_moves,
        enable_global_prio=enable_global_prio,
    )

    resolved_variant = variant or get_settings().prioritizer_variant
    spec = get_orchestrator_spec(resolved_variant)

    if on_progress:
        on_progress(f"Orchestrator '{spec.variant}' starting ({spec.cost_band} cost)")

    if overrides:
        with override_settings(**overrides):
            orch = build_orchestrator(db, broadcaster)
            await orch.run(question_id)
    else:
        orch = build_orchestrator(db, broadcaster)
        await orch.run(question_id)

    if on_progress:
        on_progress(f"Orchestrator '{spec.variant}' complete")


async def dispatch_evaluation(
    question_id: str,
    db: DB,
    *,
    eval_type: str = "default",
    broadcaster: Broadcaster | None = None,
    on_progress: Callable[[str], Any] | None = None,
) -> Call:
    """Run an evaluation agent against *question_id* and return the Call."""
    spec = get_evaluation_type_spec(eval_type)

    if on_progress:
        on_progress(f"Evaluation '{spec.name}' starting")

    call = await run_evaluation(
        question_id,
        db,
        eval_type=eval_type,
        broadcaster=broadcaster,
    )

    if on_progress:
        on_progress(f"Evaluation '{spec.name}' complete: call {call.id[:8]}")

    return call


async def dispatch_grounding_pipeline(
    pipeline: str,
    question_id: str,
    evaluation_text: str,
    db: DB,
    *,
    broadcaster: Broadcaster | None = None,
    from_stage: int = 1,
    prior_checkpoints: dict | None = None,
    on_progress: Callable[[str], Any] | None = None,
) -> Call:
    """Run a follow-up grounding/feedback pipeline on an evaluation.

    ``pipeline`` is a key in ``GROUNDING_PIPELINES`` (``grounding`` or
    ``feedback``). ``evaluation_text`` is the raw markdown that came out of
    an earlier ``run_evaluation`` (``call.review_json['evaluation']``).
    """
    spec = get_grounding_pipeline_spec(pipeline)

    if on_progress:
        on_progress(f"Grounding pipeline '{spec.name}' starting")

    call = await spec.runner(
        question_id,
        evaluation_text,
        db,
        broadcaster=broadcaster,
        from_stage=from_stage,
        prior_checkpoints=prior_checkpoints,
    )

    if on_progress:
        on_progress(f"Pipeline '{spec.name}' complete: call {call.id[:8]}")

    return call
