"""SpecCallRunner: a generic ``CallRunner`` driven by a ``CallSpec``.

Phase 1 of the CallSpec refactor. The runner resolves a spec's
``StageRef``s through the per-stage registries in ``spec.py``, plumbs
``FromCallParam`` / ``FromSettings`` / ``FromStageCtx`` sentinels in
stage configs, and plugs into the existing ``CallRunner`` machinery
so staged-runs, tracing, confusion-scan, and page-load tracking all
keep working unchanged.

Still additive: nothing imports ``SpecCallRunner`` yet. Per-spec
conversions (Phase 2+) will instantiate this class from a registered
``CallSpec``; until then it coexists with the imperative per-call-type
subclasses of ``CallRunner``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rumil.available_moves import PRESETS, get_moves_for_call
from rumil.calls.spec import (
    CLOSING_REVIEWERS,
    CONTEXT_BUILDERS,
    WORKSPACE_UPDATERS,
    AllowedMoves,
    CallSpec,
    FromCallParam,
    FromSettings,
    FromStageCtx,
    PresetKey,
    PresetOverlay,
    StageBuildCtx,
    StageFactory,
    StageRef,
)
from rumil.calls.stages import (
    CallRunner,
    ClosingReviewer,
    ContextBuilder,
    WorkspaceUpdater,
)
from rumil.database import DB
from rumil.models import Call, CallStage, CallType, MoveType
from rumil.settings import get_settings


class SpecCallRunner(CallRunner):
    """CallRunner that reads its wiring from a CallSpec rather than ClassVars."""

    def __init__(
        self,
        spec: CallSpec,
        question_id: str,
        call: Call,
        db: DB,
        *,
        broadcaster=None,
        up_to_stage: CallStage | None = None,
        max_rounds: int = 5,
        fruit_threshold: int = 4,
        stage_ctx_extras: dict[str, Any] | None = None,
    ) -> None:
        self.spec = spec
        self._stage_ctx_extras: dict[str, Any] = dict(stage_ctx_extras or {})
        # Assign call_type instance-attr before super().__init__ — the base
        # class expects a ``self.call_type`` accessor via ClassVar lookup
        # during _resolve_available_moves(); since we can't assign to a
        # ClassVar cleanly, use per-instance shadowing. Python lets us.
        self.call_type = spec.call_type  # type: ignore[misc]
        super().__init__(
            question_id,
            call,
            db,
            broadcaster=broadcaster,
            up_to_stage=up_to_stage,
            max_rounds=max_rounds,
            fruit_threshold=fruit_threshold,
        )

    def task_description(self) -> str:
        """Default task description: ``spec.description`` + scope ID.

        Matches the convention used by the imperative call-type subclasses —
        every task_description ends with ``Question ID: `<id>``` (for
        question-scoped calls) or ``Claim ID: `<id>``` (for scout_c_*
        variants that operate on a claim). The label is selected from
        ``spec.scope_page_type``.

        When ``spec.task_template`` is set, it overrides the default
        format. Substitutions available inside the template (any subset):

        - ``{scope_id}`` — the question_id / claim_id for this call
        - ``{source_page_id}`` — ``stage_ctx_extras["source_page"].id``
          when a source page was passed (used by ingest)
        - ``{settings.<name>}`` — attribute access on the active Settings
          instance (e.g. ``{settings.ingest_num_claims}``)
        """
        sid = self.infra.question_id if hasattr(self, "infra") and self.infra else None
        if self.spec.task_template is not None:
            from rumil.settings import get_settings

            source_page = self._stage_ctx_extras.get("source_page")
            return self.spec.task_template.format(
                scope_id=sid or "",
                source_page_id=getattr(source_page, "id", "") if source_page else "",
                settings=get_settings(),
            )
        base = self.spec.description.rstrip()
        if not sid:
            return base
        from rumil.models import PageType

        label = "Claim ID" if self.spec.scope_page_type == PageType.CLAIM else "Question ID"
        return f"{base}\n\n{label}: `{sid}`"

    def _make_context_builder(self) -> ContextBuilder:
        return _build_stage(
            self.spec.context_builder,
            CONTEXT_BUILDERS,
            stage_name="context_builder",
            ctx=self._build_stage_ctx(),
        )

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return _build_stage(
            self.spec.workspace_updater,
            WORKSPACE_UPDATERS,
            stage_name="workspace_updater",
            ctx=self._build_stage_ctx(),
        )

    def _make_closing_reviewer(self) -> ClosingReviewer:
        # The closing reviewer is built AFTER the workspace updater, so we
        # thread the in-progress ``self.workspace_updater`` through
        # ctx.extras. Used by the web_research reviewer factory to get
        # access to ``WebResearchLoop`` (needed so the completion summary
        # can report "N sources cited" instead of 0).
        return _build_stage(
            self.spec.closing_reviewer,
            CLOSING_REVIEWERS,
            stage_name="closing_reviewer",
            ctx=self._build_stage_ctx(
                extras_override={
                    "workspace_updater": getattr(self, "workspace_updater", None),
                },
            ),
        )

    def _resolve_available_moves(self) -> Sequence[MoveType]:
        """Resolve spec.allowed_moves to a concrete list."""
        return _resolve_allowed_moves(self.spec.allowed_moves, self.spec.call_type)

    def _build_stage_ctx(
        self,
        *,
        extras_override: dict[str, Any] | None = None,
    ) -> StageBuildCtx:
        extras = dict(self._stage_ctx_extras)
        if extras_override:
            extras.update(extras_override)
        return StageBuildCtx(
            call_type=self.spec.call_type,
            question_id=self.infra.question_id if hasattr(self, "infra") else None,
            task_description=self.task_description(),
            available_moves=_resolve_allowed_moves(self.spec.allowed_moves, self.spec.call_type),
            source_page=extras.get("source_page"),
            extras=extras,
        )


def _build_stage(
    ref: StageRef,
    registry: dict[str, StageFactory],
    *,
    stage_name: str,
    ctx: StageBuildCtx,
) -> Any:
    factory = registry.get(ref.id)
    if factory is None:
        raise ValueError(f"{stage_name} id '{ref.id}' is not registered. Known: {sorted(registry)}")
    resolved = _resolve_config(ref.config, ctx=ctx)
    return factory(ctx, resolved)


def _resolve_config(config: dict[str, Any], *, ctx: StageBuildCtx) -> dict[str, Any]:
    """Resolve FromCallParam / FromSettings / FromStageCtx sentinels to values.

    Literal values pass through unchanged. Unknown sentinel types raise
    ``TypeError`` — specs shouldn't accumulate a DSL; stick to the three
    documented sentinels and escape to a bespoke stage if more is needed.
    """
    out: dict[str, Any] = {}
    for key, raw in config.items():
        out[key] = _resolve_value(raw, ctx=ctx)
    return out


def _resolve_value(value: Any, *, ctx: StageBuildCtx) -> Any:
    if isinstance(value, FromCallParam):
        # call_params live on the Call; the runner has already created it,
        # but sentinels are resolved before infra exists. We accept the
        # default here and let stages query call_params at runtime if they
        # need per-call overrides beyond what the spec pinned.
        return value.default
    if isinstance(value, FromSettings):
        settings = get_settings()
        return getattr(settings, value.name)
    if isinstance(value, FromStageCtx):
        return getattr(ctx, value.attr)
    return value


def _resolve_allowed_moves(allowed: AllowedMoves, call_type: CallType) -> Sequence[MoveType]:
    if isinstance(allowed, PresetKey):
        # PresetKey.key is a CallType name (string form of the enum), letting
        # a spec borrow another call type's preset entry. Default case is
        # spec.call_type itself; falling through to get_moves_for_call lets
        # that flow through settings.available_moves / enable_flag_issue /
        # enable_annotation_moves uniformly.
        borrow_ct = call_type
        if allowed.key:
            try:
                borrow_ct = CallType(allowed.key)
            except ValueError as exc:
                preset_name = get_settings().available_moves
                raise ValueError(
                    f"CallSpec PresetKey('{allowed.key}') is not a known CallType; "
                    f"active preset is '{preset_name}' / known PRESETS={sorted(PRESETS)}"
                ) from exc
        return list(get_moves_for_call(borrow_ct))
    if isinstance(allowed, PresetOverlay):
        base = _resolve_allowed_moves(allowed.base, call_type)
        result = [m for m in base if m not in allowed.remove]
        for extra in allowed.add:
            if extra not in result:
                result.append(extra)
        return result
    # tuple[MoveType, ...]
    return list(allowed)
