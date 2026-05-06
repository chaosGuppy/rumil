"""Atlas description-completeness lint.

These checks turn the descriptions feeding the LLM and the atlas UI
into load-bearing data: a missing field-level description, an
out-of-sync workflow-stage prompt reference, or a CallType added
without a canonical description fails CI rather than silently producing
noisy LLM behaviour and gappy docs.
"""

from __future__ import annotations

from rumil.atlas.descriptions import (
    CALL_TYPE_DESCRIPTIONS,
    PAGE_LAYER_DESCRIPTIONS,
    PAGE_TYPE_DESCRIPTIONS,
    WORKSPACE_DESCRIPTIONS,
)
from rumil.atlas.registry import (
    build_call_type_summaries,
    build_dispatch_summaries,
    build_move_summaries,
)
from rumil.atlas.workflows import all_profiles
from rumil.calls.dispatches import (
    DISPATCH_DEFS,
    RECURSE_CLAIM_DISPATCH_DEF,
    RECURSE_DISPATCH_DEF,
)
from rumil.models import (
    DISPATCHABLE_CALL_TYPES,
    CallType,
    MoveType,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.moves.registry import MOVES
from rumil.prompts import PROMPTS_DIR


def test_every_move_type_has_a_registered_def():
    missing = [mt.value for mt in MoveType if mt not in MOVES]
    assert missing == [], f"MoveType values without a registered MoveDef: {missing}"


def test_every_move_def_has_non_empty_description():
    bad = [(mt.value, m.name) for mt, m in MOVES.items() if not m.description.strip()]
    assert bad == [], f"MoveDefs with empty description: {bad}"


def test_every_move_payload_field_has_a_description():
    """Pydantic Field(description=) is what the LLM sees as the tool input
    schema docstring. Every field on a move payload must have one."""
    bad: list[tuple[str, str]] = []
    for move_type, move in MOVES.items():
        schema = move.schema.model_json_schema()
        for name, fs in (schema.get("properties") or {}).items():
            if not (fs.get("description") or "").strip():
                bad.append((move_type.value, name))
    assert bad == [], f"Move payload fields missing description= : {bad}"


def test_every_dispatchable_call_type_has_a_dispatch_def():
    missing = [ct.value for ct in DISPATCHABLE_CALL_TYPES if ct not in DISPATCH_DEFS]
    assert missing == [], f"CallType in DISPATCHABLE_CALL_TYPES without a DispatchDef: {missing}"


def test_every_dispatch_def_has_non_empty_description():
    bad: list[str] = []
    for ct, d in DISPATCH_DEFS.items():
        if not d.description.strip():
            bad.append(ct.value)
    for d in (RECURSE_DISPATCH_DEF, RECURSE_CLAIM_DISPATCH_DEF):
        if not d.description.strip():
            bad.append(d.name)
    assert bad == [], f"DispatchDefs with empty description: {bad}"


def test_every_dispatch_payload_field_has_a_description():
    bad: list[tuple[str, str]] = []
    targets = [*DISPATCH_DEFS.values(), RECURSE_DISPATCH_DEF, RECURSE_CLAIM_DISPATCH_DEF]
    for d in targets:
        schema = d.schema.model_json_schema()
        for name, fs in (schema.get("properties") or {}).items():
            # question_id on ScopeOnlyDispatchPayload is injected at runtime;
            # its description being empty is acceptable but we still flag
            # other empty descriptions on dispatch fields.
            if name == "question_id":
                continue
            if not (fs.get("description") or "").strip():
                bad.append((d.name, name))
    assert bad == [], f"Dispatch payload fields missing description= : {bad}"


def test_every_call_type_has_a_canonical_description():
    missing = [ct.value for ct in CallType if not CALL_TYPE_DESCRIPTIONS.get(ct, "").strip()]
    assert missing == [], (
        f"CallType values without a canonical description in atlas.descriptions: {missing}"
    )


def test_every_page_type_has_a_canonical_description():
    missing = [pt.value for pt in PageType if not PAGE_TYPE_DESCRIPTIONS.get(pt, "").strip()]
    assert missing == [], (
        f"PageType values without a canonical description in atlas.descriptions: {missing}"
    )


def test_every_page_layer_has_a_canonical_description():
    missing = [ly.value for ly in PageLayer if not PAGE_LAYER_DESCRIPTIONS.get(ly, "").strip()]
    assert missing == [], f"PageLayer values without a canonical description: {missing}"


def test_every_workspace_has_a_canonical_description():
    missing = [ws.value for ws in Workspace if not WORKSPACE_DESCRIPTIONS.get(ws, "").strip()]
    assert missing == [], f"Workspace values without a canonical description: {missing}"


def test_every_workflow_stage_prompt_file_exists():
    """Each WorkflowStage's prompt_files must point at real prompts/*.md."""
    bad: list[tuple[str, str, str]] = []
    for profile in all_profiles():
        for stage in profile.stages:
            for prompt in stage.prompt_files:
                if not (PROMPTS_DIR / prompt).exists():
                    bad.append((profile.name, stage.id, prompt))
    assert bad == [], f"Workflow stages referencing missing prompts: {bad}"


def test_every_workflow_stage_dispatch_call_type_is_registered():
    """Every dispatch call_type referenced by a workflow stage must be a
    real CallType, the literal 'recurse' / 'recurse_claim' label, or in
    DISPATCH_DEFS."""
    valid_ct_values = {ct.value for ct in CallType}
    valid_recurses = {"recurse", "recurse_claim"}
    bad: list[tuple[str, str, str]] = []
    for profile in all_profiles():
        for stage in profile.stages:
            for ct in stage.available_dispatch_call_types:
                if ct in valid_recurses:
                    continue
                if ct not in valid_ct_values:
                    bad.append((profile.name, stage.id, ct))
    assert bad == [], f"Unknown dispatch call_types referenced: {bad}"


def test_every_call_type_summary_is_reachable():
    """Smoke check: build the registry rollups without raising."""
    moves = build_move_summaries()
    dispatches = build_dispatch_summaries()
    calls = build_call_type_summaries()
    assert len(moves) == len(MoveType), (
        f"move summaries={len(moves)} but MoveType has {len(MoveType)}"
    )
    assert len(calls) == len(CallType), (
        f"call type summaries={len(calls)} but CallType has {len(CallType)}"
    )
    assert len(dispatches) >= len(DISPATCH_DEFS) + 2, (
        f"dispatch summaries={len(dispatches)} should include "
        f"{len(DISPATCH_DEFS)} + recurse + recurse_claim"
    )


def test_every_workflow_recurse_target_resolves_to_a_known_workflow():
    names = {p.name for p in all_profiles()}
    bad: list[tuple[str, str]] = []
    for profile in all_profiles():
        for target in profile.recurses_into:
            if target not in names:
                bad.append((profile.name, target))
        for stage in profile.stages:
            for target in stage.recurses_into:
                if target not in names:
                    bad.append((profile.name, target))
    assert bad == [], f"Workflow recurse targets that don't resolve: {bad}"
