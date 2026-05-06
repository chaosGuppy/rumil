"""Atlas description-completeness lint.

These checks turn the descriptions feeding the LLM and the atlas UI
into load-bearing data: a missing field-level description, an
out-of-sync workflow-stage prompt reference, or a CallType added
without a canonical description fails CI rather than silently producing
noisy LLM behaviour and gappy docs.
"""

from __future__ import annotations

from pathlib import Path

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


def test_every_atlas_event_string_matches_its_class_literal():
    """Atlas reads trace events by string discriminator. Each constant
    in ``atlas.event_keys.ATLAS_READS`` must equal the corresponding
    Pydantic class's ``event`` Literal default — a rename in
    ``trace_events.py`` should fail here, not silently zero a counter."""
    from rumil.atlas import event_keys

    bad: list[tuple[str, str, str]] = []
    for literal, cls in event_keys.ATLAS_READS.items():
        cls_default = cls.model_fields["event"].default
        if cls_default != literal:
            bad.append((cls.__name__, literal, str(cls_default)))
    assert bad == [], f"event_keys constants out of sync with trace_event class Literals: {bad}"


def test_every_workflow_has_stage_attribution_or_is_event_keyed():
    """A WorkflowProfile that's expected to have call-row-attributable
    stages should appear in atlas.aggregate's _EXECUTE_BY_WORKFLOW (or
    _DELEGATE_WORKFLOW). DAE / reflective_judge are deliberately
    event-keyed (their stages live as trace events on a single call,
    not as separate calls); they go on an explicit allowlist below.
    Anything else should fail loudly here rather than silently return
    stages_taken=[] in production."""
    from rumil.atlas.aggregate import (
        _DELEGATE_WORKFLOW,
        _EXECUTE_BY_WORKFLOW,
    )

    event_keyed_allowlist = {
        "draft_and_edit",
        "reflective_judge",
        "global_prio",
    }
    bad: list[str] = []
    for profile in all_profiles():
        name = profile.name
        if name in event_keyed_allowlist:
            continue
        if name in _EXECUTE_BY_WORKFLOW:
            continue
        if name in _DELEGATE_WORKFLOW:
            continue
        bad.append(name)
    assert bad == [], (
        f"WorkflowProfiles without stage attribution wiring: {bad}. "
        "Either add an _EXECUTE_BY_WORKFLOW entry, a _DELEGATE_WORKFLOW "
        "entry, or add the workflow to the event_keyed allowlist in this "
        "test if its stages live in trace events on a single call."
    )


def test_every_workflow_loop_pair_resolves_to_real_stages():
    """The hardcoded ``loop_pairs`` in aggregate._rollup_run links a
    loop stage to the body stages whose firing means the loop fired.
    Both the loop_id and every body stage_id must be a real stage in
    the corresponding workflow's profile."""
    profiles_by_name = {p.name: p for p in all_profiles()}
    loop_pairs = {
        "two_phase": ("main_phase_loop", {"main_phase_prioritization"}),
        "experimental": (
            "experimental_prio_loop",
            {"experimental_prioritization"},
        ),
        "claim_investigation": (
            "claim_main_loop",
            {"claim_phase2_prioritization"},
        ),
        "draft_and_edit": (
            "dae_round_loop",
            {"dae_draft", "dae_critique"},
        ),
    }
    bad: list[str] = []
    for workflow_name, (loop_id, body) in loop_pairs.items():
        profile = profiles_by_name.get(workflow_name)
        if profile is None:
            bad.append(f"{workflow_name}: profile not found")
            continue
        stage_ids = {s.id for s in profile.stages}
        if loop_id not in stage_ids:
            bad.append(f"{workflow_name}: loop_id {loop_id!r} not in profile stages")
        for s in body:
            if s not in stage_ids:
                bad.append(f"{workflow_name}: body stage {s!r} not in profile stages")
    assert bad == [], f"loop_pairs reference unknown stage ids: {bad}"


def test_every_workflow_code_path_exists_on_disk():
    repo_root = Path(__file__).resolve().parents[1]
    bad: list[tuple[str, str]] = []
    for profile in all_profiles():
        for code_path in profile.code_paths:
            if not (repo_root / code_path).exists():
                bad.append((profile.name, code_path))
    assert bad == [], f"WorkflowProfile code_paths missing on disk: {bad}"


def test_every_workflow_relevant_setting_exists_on_settings():
    """relevant_settings drives fingerprint dimensions. A renamed
    Settings field silently drops the dimension if not caught."""
    from rumil.settings import Settings

    fields = set(Settings.model_fields.keys())
    bad: list[tuple[str, str]] = []
    for profile in all_profiles():
        for s in profile.relevant_settings:
            if s not in fields:
                bad.append((profile.name, s))
    assert bad == [], f"relevant_settings names not on Settings: {bad}"


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
