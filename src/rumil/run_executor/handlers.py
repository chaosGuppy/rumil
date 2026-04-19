"""Default run handlers wrapping the existing dispatch_* coroutines.

Imported for side effects: each ``@register_handler`` call installs
itself into the module-level ``_KIND_HANDLERS`` in ``executor.py``.
``RunExecutor.start()`` looks up handlers by ``RunSpec.kind``; callers
should ``import rumil.run_executor.handlers`` (or
``from rumil.run_executor import executor``, which transitively imports
this module via ``__init__``) before calling ``start()``.

Each handler unpacks kind-specific args from ``spec.payload``. Unknown
keys are ignored; missing required keys raise ValueError so callers get
a clear failure instead of a confusing dispatch error deep in the stack.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

# NOTE: ``rumil.dispatch`` is imported lazily *inside* each handler so
# that loading ``rumil.run_executor`` (which includes these handlers via
# the package __init__) doesn't transitively pull in
# ``rumil.orchestrators``. The orchestrator modules in turn want to
# ``import RunExecutor``; resolving that while the executor package is
# mid-load triggers a circular import. Lazy per-call imports keep the
# package-load graph a DAG.
from rumil.models import CallType
from rumil.run_executor.executor import register_handler
from rumil.run_executor.run_spec import RunSpec

if TYPE_CHECKING:
    from rumil.database import DB


def _require(payload: dict[str, Any], key: str, kind: str) -> Any:
    if key not in payload:
        raise ValueError(
            f"RunSpec(kind={kind!r}) requires payload[{key!r}]; got payload keys={sorted(payload)}"
        )
    return payload[key]


@register_handler("orchestrator")
async def _handle_orchestrator(spec: RunSpec, db: DB) -> None:
    """``spec.question_id`` is required; optional payload keys: variant,
    available_calls, available_moves, enable_global_prio.
    """
    from rumil.dispatch import dispatch_orchestrator

    if not spec.question_id:
        raise ValueError("RunSpec(kind='orchestrator') requires spec.question_id")
    payload = spec.payload
    await dispatch_orchestrator(
        spec.question_id,
        db,
        variant=payload.get("variant"),
        available_calls=payload.get("available_calls"),
        available_moves=payload.get("available_moves"),
        enable_global_prio=payload.get("enable_global_prio"),
    )


@register_handler("evaluation")
async def _handle_evaluation(spec: RunSpec, db: DB) -> None:
    """``spec.question_id`` required; payload: eval_type (default "default")."""
    from rumil.dispatch import dispatch_evaluation

    if not spec.question_id:
        raise ValueError("RunSpec(kind='evaluation') requires spec.question_id")
    await dispatch_evaluation(
        spec.question_id,
        db,
        eval_type=spec.payload.get("eval_type", "default"),
    )


@register_handler("single_call")
async def _handle_single_call(spec: RunSpec, db: DB) -> None:
    """payload must carry ``call_type`` (str matching CallType value);
    ``spec.question_id`` required. Optional: max_rounds, model, origin,
    extra_runner_kwargs.
    """
    from rumil.dispatch import dispatch_single_call

    if not spec.question_id:
        raise ValueError("RunSpec(kind='single_call') requires spec.question_id")
    call_type_raw = _require(spec.payload, "call_type", "single_call")
    call_type = call_type_raw if isinstance(call_type_raw, CallType) else CallType(call_type_raw)
    await dispatch_single_call(
        call_type,
        spec.question_id,
        db,
        max_rounds=spec.payload.get("max_rounds"),
        model=spec.payload.get("model"),
        origin=spec.payload.get("origin"),
        extra_runner_kwargs=spec.payload.get("extra_runner_kwargs"),
    )


@register_handler("grounding_pipeline")
async def _handle_grounding_pipeline(spec: RunSpec, db: DB) -> None:
    """payload: pipeline (required, str), evaluation_text (required, str),
    from_stage (optional int, default 1), prior_checkpoints (optional dict).
    ``spec.question_id`` required.
    """
    from rumil.dispatch import dispatch_grounding_pipeline

    if not spec.question_id:
        raise ValueError("RunSpec(kind='grounding_pipeline') requires spec.question_id")
    await dispatch_grounding_pipeline(
        _require(spec.payload, "pipeline", "grounding_pipeline"),
        spec.question_id,
        _require(spec.payload, "evaluation_text", "grounding_pipeline"),
        db,
        from_stage=int(spec.payload.get("from_stage", 1)),
        prior_checkpoints=spec.payload.get("prior_checkpoints"),
    )
