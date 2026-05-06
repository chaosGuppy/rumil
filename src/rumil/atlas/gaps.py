"""Atlas /gaps detector — surfaces inconsistencies in the live registry.

Statics that can be checked at request time:

- CallTypes without a registered ``CallRunner`` subclass (and without
  being PRIORITIZATION-style).
- Settings declared ``relevant_settings`` on a workflow that don't
  exist on ``rumil.settings.Settings``.
- Prompt files in ``prompts/`` that nothing in atlas references
  (neither call-type composition nor workflow stage).
- ``MoveType`` values absent from every available-moves preset.
- Workflow ``code_paths`` that don't exist on disk.
- DispatchableCallType values without a ``DispatchDef``.

Cheap to compute on every request — no DB access. The ``GapsReport``
output is consumed by the ``/atlas/gaps`` UI route.
"""

from __future__ import annotations

from pathlib import Path

from rumil.atlas.descriptions import CALL_TYPE_DESCRIPTIONS
from rumil.atlas.prompt_parts import build_prompt_composition
from rumil.atlas.registry import _runner_index
from rumil.atlas.schemas import GapItem, GapsReport
from rumil.atlas.workflows import all_profiles
from rumil.available_moves import PRESETS as MOVES_PRESETS
from rumil.calls.dispatches import DISPATCH_DEFS
from rumil.models import DISPATCHABLE_CALL_TYPES, CallType, MoveType, PageType
from rumil.moves.registry import MOVES
from rumil.prompts import PROMPTS_DIR

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _settings_field_names() -> set[str]:
    from rumil.settings import Settings

    return set(Settings.model_fields.keys())


def _all_referenced_prompts() -> set[str]:
    """Every prompt file name that any call-type composition or workflow
    stage points at."""
    refs: set[str] = set()
    for ct in CallType:
        comp = build_prompt_composition(ct.value)
        for part in comp.parts:
            if part.exists:
                refs.add(part.name)
    for profile in all_profiles():
        for stage in profile.stages:
            for fname in stage.prompt_files:
                if (PROMPTS_DIR / fname).exists():
                    refs.add(fname)
    return refs


def build_gaps_report() -> GapsReport:
    items: list[GapItem] = []

    runner_idx = _runner_index()
    for ct in CallType:
        if ct in runner_idx:
            continue
        # PRIORITIZATION variants and a few envelope types intentionally
        # don't have CallRunner classes.
        whitelisted = {
            CallType.PRIORITIZATION,
            CallType.GLOBAL_PRIORITIZATION,
            CallType.CLAUDE_CODE_DIRECT,
            CallType.RED_TEAM,
            CallType.AB_EVAL,
            CallType.AB_EVAL_COMPARISON,
            CallType.AB_EVAL_SUMMARY,
            CallType.RUN_EVAL,
            CallType.GROUNDING_FEEDBACK,
            CallType.FEEDBACK_UPDATE,
            CallType.MAINTAIN,
            CallType.REFRAME,
            CallType.SUMMARIZE,
            CallType.VERSUS_JUDGE,
            CallType.VERSUS_COMPLETE,
            CallType.UPDATE_VIEW,
            CallType.UPDATE_VIEW_MAX_EFFORT,
            CallType.UPDATE_FREEFORM_VIEW,
            CallType.CREATE_VIEW_MAX_EFFORT,
            CallType.EVALUATE,
            CallType.LINK_SUBQUESTIONS,
        }
        if ct in whitelisted:
            continue
        items.append(
            GapItem(
                kind="call_type_without_runner",
                target=ct.value,
                detail=(
                    "No CallRunner subclass declares call_type = "
                    f"CallType.{ct.name}; expected one in src/rumil/calls/."
                ),
                href=f"/atlas/calls/{ct.value}",
            )
        )

    for ct in DISPATCHABLE_CALL_TYPES:
        if ct not in DISPATCH_DEFS:
            items.append(
                GapItem(
                    kind="dispatchable_without_dispatch_def",
                    target=ct.value,
                    detail=(
                        "DISPATCHABLE_CALL_TYPES lists this CallType but "
                        "DISPATCH_DEFS has no corresponding entry."
                    ),
                    href=f"/atlas/calls/{ct.value}",
                )
            )

    settings_fields = _settings_field_names()
    for profile in all_profiles():
        for s in profile.relevant_settings:
            if s not in settings_fields:
                items.append(
                    GapItem(
                        kind="workflow_setting_missing",
                        target=f"{profile.name}.{s}",
                        detail=(
                            f"Workflow {profile.name!r} declares "
                            f"relevant_settings={s!r} but Settings has no "
                            f"such field."
                        ),
                        href=f"/atlas/workflows/{profile.name}",
                    )
                )

    for profile in all_profiles():
        for path_str in profile.code_paths:
            full = _REPO_ROOT / path_str
            if not full.exists():
                items.append(
                    GapItem(
                        kind="workflow_code_path_missing",
                        target=f"{profile.name}: {path_str}",
                        detail=(
                            f"Workflow {profile.name!r} declares code_paths "
                            f"entry {path_str!r} but the file/dir is missing."
                        ),
                        href=f"/atlas/workflows/{profile.name}",
                    )
                )

    referenced = _all_referenced_prompts()
    on_disk = {p.name for p in PROMPTS_DIR.glob("*.md")}
    for orphan in sorted(on_disk - referenced):
        items.append(
            GapItem(
                kind="orphan_prompt_file",
                target=orphan,
                detail=(
                    f"prompts/{orphan} exists but no call-type composition "
                    f"or workflow stage references it. Either remove the "
                    f"file or add a reference."
                ),
                href=f"/atlas/prompts/{orphan}",
            )
        )

    moves_in_presets: set[MoveType] = set()
    for preset in MOVES_PRESETS.values():
        for move_list in preset.values():
            moves_in_presets.update(move_list)
    for mt in MoveType:
        if mt in MOVES and mt not in moves_in_presets:
            items.append(
                GapItem(
                    kind="move_in_no_preset",
                    target=mt.value,
                    detail=(
                        f"{mt.value} has a registered MoveDef but isn't "
                        "listed in any available-moves preset, so no call "
                        "type can use it."
                    ),
                    href=f"/atlas/moves/{mt.value}",
                )
            )

    for ct in CallType:
        desc = CALL_TYPE_DESCRIPTIONS.get(ct, "")
        if not desc.strip():
            items.append(
                GapItem(
                    kind="call_type_without_description",
                    target=ct.value,
                    detail="atlas.descriptions has no canonical description.",
                    href=f"/atlas/calls/{ct.value}",
                )
            )
    for pt in PageType:
        from rumil.atlas.descriptions import PAGE_TYPE_DESCRIPTIONS

        desc = PAGE_TYPE_DESCRIPTIONS.get(pt, "")
        if not desc.strip():
            items.append(
                GapItem(
                    kind="page_type_without_description",
                    target=pt.value,
                    detail="atlas.descriptions has no canonical description.",
                    href=f"/atlas/pages/{pt.value}",
                )
            )

    counts: dict[str, int] = {}
    for it in items:
        counts[it.kind] = counts.get(it.kind, 0) + 1
    return GapsReport(items=items, counts_by_kind=counts)
