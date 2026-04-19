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

import inspect
from collections.abc import Callable
from typing import Any

from rumil.calls.call_registry import (
    ASSESS_CALL_CLASSES,
    get_call_runner_class,
)
from rumil.database import DB
from rumil.evaluate.registry import (
    get_evaluation_type_spec,
    get_grounding_pipeline_spec,
)
from rumil.evaluate.runner import run_evaluation
from rumil.models import Call, CallType
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


async def dispatch_single_call(
    call_type: CallType,
    question_id: str,
    db: DB,
    *,
    max_rounds: int | None = None,
    model: str | None = None,
    broadcaster: Broadcaster | None = None,
    on_progress: Callable[[str], Any] | None = None,
    origin: str | None = None,
    extra_runner_kwargs: dict[str, Any] | None = None,
) -> Call:
    """Fire one dispatchable call on *question_id* and return the saved Call.

    Resolves the runner class via ``get_call_runner_class`` (or
    ``ASSESS_CALL_CLASSES[settings.assess_call_variant]`` for assess).
    Filters ``max_rounds``, ``broadcaster``, and any ``extra_runner_kwargs``
    through ``inspect.signature`` so callers don't have to special-case
    runners that don't accept them (WebResearchCall, CreateViewCall, etc.).

    Tagging: when ``origin`` is provided, the call's ``call_params`` get
    ``{"origin": "claude-code", "skill": origin}`` merged in so the run is
    distinguishable from a ``main.py`` invocation in later analyses.

    Caller owns the DB, run record, and budget setup. Progress messages
    are coarse ("starting", "complete") — the runner itself broadcasts
    rich trace events to ``trace:{run_id}`` via the optional broadcaster.
    """
    settings = get_settings()

    if call_type == CallType.ASSESS:
        variant = settings.assess_call_variant
        cls: type[Any] | None = ASSESS_CALL_CLASSES.get(variant)
        if cls is None:
            raise ValueError(
                f"Unknown assess_call_variant {variant!r}. Available: {sorted(ASSESS_CALL_CLASSES)}"
            )
    else:
        cls = get_call_runner_class(call_type)

    call = await db.create_call(call_type, scope_page_id=question_id)
    if origin:
        call.call_params = {
            **(call.call_params or {}),
            "origin": "claude-code",
            "skill": origin,
        }
        await db.save_call(call)

    sig = inspect.signature(cls.__init__)
    accepted: set[str] = set(sig.parameters.keys())

    kwargs: dict[str, Any] = {}
    if max_rounds is not None and "max_rounds" in accepted:
        kwargs["max_rounds"] = max_rounds
    if broadcaster is not None and "broadcaster" in accepted:
        kwargs["broadcaster"] = broadcaster
    for k, v in (extra_runner_kwargs or {}).items():
        if k in accepted:
            kwargs[k] = v

    runner = cls(question_id, call, db, **kwargs)

    if on_progress:
        on_progress(f"{call_type.value} call {call.id[:8]} starting")

    if model:
        with override_settings(rumil_model_override=model):
            await runner.run()
    else:
        await runner.run()

    if on_progress:
        on_progress(f"{call_type.value} call {call.id[:8]} complete")

    refreshed = await db.get_call(call.id)
    return refreshed or call


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
