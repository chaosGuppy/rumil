"""Workflow profiles: declarative descriptions of each orchestrator / versus
workflow's stages, branches, prompts, and recursion structure.

The orchestrators themselves are imperative (the ``while True`` over
``get_dispatches`` / ``execute_dispatches`` shape — see
``rumil.orchestrators.two_phase``), so the spec lives next to them
rather than inside them. A description-completeness test in
``tests/test_atlas_descriptions.py`` asserts every prompt referenced
here exists on disk and every available-dispatch refers to a registered
``DispatchDef``, so drift between the spec and the underlying code shows
up as a CI failure.

Adding a new workflow: append a ``WorkflowProfile`` to ``_PROFILES``. A
workflow's ``stages`` are read in declaration order and rendered as a
linear pipeline by the atlas UI, with optional/loop/branch flags
controlling presentation. ``recurses_into`` lists workflow names this
workflow can recurse into — used to draw cross-workflow links in the
diagram.
"""

from __future__ import annotations

from rumil.atlas.schemas import (
    WorkflowProfile,
    WorkflowStage,
    WorkflowSummary,
)
from rumil.available_calls import AVAILABLE_CALLS_PRESETS
from rumil.available_moves import PRESETS as AVAILABLE_MOVES_PRESETS
from rumil.models import CallType


def _initial_scouts(preset_name: str = "default") -> list[str]:
    preset = AVAILABLE_CALLS_PRESETS.get(preset_name)
    if preset is None:
        return []
    return [c.value for c in preset.initial_prioritization_scouts]


def _main_phase_dispatch(preset_name: str = "default") -> list[str]:
    preset = AVAILABLE_CALLS_PRESETS.get(preset_name)
    if preset is None:
        return []
    return [c.value for c in preset.main_phase_prioritization_dispatch]


def _claim_phase1_scouts(preset_name: str = "default") -> list[str]:
    preset = AVAILABLE_CALLS_PRESETS.get(preset_name)
    if preset is None:
        return []
    return [c.value for c in preset.claim_phase1_scouts]


def _claim_phase2_dispatch(preset_name: str = "default") -> list[str]:
    preset = AVAILABLE_CALLS_PRESETS.get(preset_name)
    if preset is None:
        return []
    return [c.value for c in preset.claim_phase2_dispatch]


def _moves_for(call_type: CallType, preset_name: str = "default") -> list[str]:
    preset = AVAILABLE_MOVES_PRESETS.get(preset_name)
    if preset is None:
        return []
    return [m.value for m in preset.get(call_type, ())]


_TWO_PHASE_PROFILE = WorkflowProfile(
    name="two_phase",
    kind="orchestrator",
    summary=(
        "Two-phase orchestrator for new questions. An initial scout fan-out "
        "round generates breadth (subquestions, estimates, hypotheses, "
        "analogies, paradigm cases, factchecks), then a main-phase loop "
        "scores existing items by impact and remaining fruit and dispatches "
        "targeted follow-up — more scouts, web research, or recursive "
        "investigation of subquestions or specific claims."
    ),
    code_paths=[
        "src/rumil/orchestrators/two_phase.py",
        "src/rumil/orchestrators/base.py",
        "src/rumil/orchestrators/common.py",
        "src/rumil/orchestrators/dispatch_handlers.py",
    ],
    relevant_settings=[
        "available_moves",
        "available_calls",
        "assess_call_variant",
        "enable_red_team",
        "enable_global_prio",
        "subquestion_linker_enabled",
        "prioritizer_variant",
        "view_variant",
        "budget_pacing_enabled",
    ],
    stages=[
        WorkflowStage(
            id="initial_prioritization",
            label="Initial prioritization",
            description=(
                "First round only, and only if the question doesn't already "
                "have a View. Fans out specialized scouts in parallel for "
                "breadth coverage of the question."
            ),
            prompt_files=["two_phase_initial_prioritization.md"],
            available_dispatch_call_types=_initial_scouts(),
            optional=True,
            branch_condition="no existing View on the question",
        ),
        WorkflowStage(
            id="main_phase_loop",
            label="Main-phase loop",
            description=(
                "Repeats until budget is exhausted or no further dispatches "
                "are produced. Each iteration: score existing subquestions "
                "and considerations by impact + remaining fruit, then "
                "dispatch the next round of work."
            ),
            loop=True,
        ),
        WorkflowStage(
            id="main_phase_prioritization",
            label="Main-phase prioritization",
            description=(
                "Per-loop iteration. Reads existing subquestions, "
                "considerations, and per-scout-type remaining fruit; emits "
                "a budgeted dispatch plan including possible recurses into "
                "subquestions or claim investigations."
            ),
            prompt_files=["two_phase_main_phase_prioritization.md"],
            available_dispatch_call_types=[
                *_main_phase_dispatch(),
                "recurse",
                "recurse_claim",
            ],
            recurses_into=["two_phase", "claim_investigation"],
        ),
        WorkflowStage(
            id="execute_dispatches",
            label="Execute dispatches",
            description=(
                "Dispatched calls run in parallel. Calls targeting a "
                "subquestion get an automatic view-refresh appended."
            ),
        ),
        WorkflowStage(
            id="view_refresh",
            label="View refresh",
            description=(
                "After non-first iterations or on the final call, refresh "
                "the question's View page so the surface answer reflects "
                "the latest research."
            ),
            optional=True,
            branch_condition="invocation > 1 or last_call",
        ),
        WorkflowStage(
            id="red_team",
            label="Red team",
            description=(
                "Adversarial pass over the current judgement, surfacing "
                "missed considerations and stress tests."
            ),
            optional=True,
            branch_condition="settings.enable_red_team",
        ),
    ],
    recurses_into=["two_phase", "claim_investigation"],
    fingerprint_keys=["budget", "settings.*"],
    notes=[
        "The PRIORITIZATION call itself uses no moves — its tools are the "
        "dispatch_* tools that record planned dispatches.",
        "Recurses pre-register their budget contribution against the "
        "child question's pool atomically, so peer cycles see correct "
        "remaining budget.",
    ],
)


_CLAIM_INVESTIGATION_PROFILE = WorkflowProfile(
    name="claim_investigation",
    kind="orchestrator",
    summary=(
        "Two-phase orchestrator focused on investigating one specific "
        "claim. Phase 1 fans out claim-specific scouts (how-true, "
        "how-false, cruxes, relevant-evidence, stress-test cases, "
        "robustify); phase 2 scores findings and dispatches targeted "
        "follow-up — strengthen, more scouts, find_considerations, or "
        "recurse into sub-investigations."
    ),
    code_paths=[
        "src/rumil/orchestrators/claim_investigation.py",
        "src/rumil/orchestrators/base.py",
        "src/rumil/orchestrators/common.py",
    ],
    relevant_settings=[
        "available_moves",
        "available_calls",
        "assess_call_variant",
        "budget_pacing_enabled",
    ],
    stages=[
        WorkflowStage(
            id="claim_phase1",
            label="Phase 1: claim-scout fan-out",
            description=(
                "First round. Dispatches claim-specific scouts in parallel "
                "to surface evidence for and against the focal claim."
            ),
            prompt_files=["claim_investigation_p1.md"],
            available_dispatch_call_types=_claim_phase1_scouts(),
            optional=True,
            branch_condition="no existing judgement on the claim",
        ),
        WorkflowStage(
            id="claim_main_loop",
            label="Phase 2 loop",
            description="Score, dispatch, repeat until budget exhausted.",
            loop=True,
        ),
        WorkflowStage(
            id="claim_phase2_prioritization",
            label="Phase 2 prioritization",
            description=(
                "Score scout fruit + considerations + sub-claims, dispatch "
                "the next batch of work (more scouts, find_considerations, "
                "or recurse into related claims/questions)."
            ),
            prompt_files=["claim_investigation_p2.md"],
            available_dispatch_call_types=[
                *_claim_phase2_dispatch(),
                "recurse",
                "recurse_claim",
            ],
            recurses_into=["two_phase", "claim_investigation"],
        ),
        WorkflowStage(
            id="claim_execute",
            label="Execute dispatches",
            description="Concurrent execution of the planned dispatches.",
        ),
    ],
    recurses_into=["two_phase", "claim_investigation"],
    fingerprint_keys=["budget", "settings.*"],
    notes=[
        "Distinct prompt set from two_phase — focal claim is the scope "
        "page rather than a question.",
    ],
)


_EXPERIMENTAL_PROFILE = WorkflowProfile(
    name="experimental",
    kind="orchestrator",
    summary=(
        "Experimental orchestrator variant — same overall shape as "
        "two_phase but with a different scoring pass and a single-phase "
        "prioritizer. Used to A/B test prioritization changes against "
        "two_phase on the same questions."
    ),
    code_paths=[
        "src/rumil/orchestrators/experimental.py",
        "src/rumil/orchestrators/base.py",
        "src/rumil/orchestrators/common.py",
    ],
    relevant_settings=[
        "available_moves",
        "available_calls",
        "assess_call_variant",
        "enable_red_team",
        "budget_pacing_enabled",
    ],
    stages=[
        WorkflowStage(
            id="experimental_prio_loop",
            label="Prioritization loop",
            description=(
                "Each iteration scores open work, dispatches a batch, "
                "and refreshes the View when warranted."
            ),
            loop=True,
        ),
        WorkflowStage(
            id="experimental_prioritization",
            label="Experimental prioritization",
            description=(
                "Single-prompt prioritizer that scores subquestions and claim items together."
            ),
            prompt_files=[],
            note=(
                "Single-prompt prioritizer; the prompt file is selected at "
                "runtime by the orchestrator based on its phase context, "
                "not declared per-stage here."
            ),
            available_dispatch_call_types=[
                *_main_phase_dispatch(),
                "recurse",
                "recurse_claim",
            ],
            recurses_into=["experimental", "claim_investigation"],
        ),
        WorkflowStage(
            id="experimental_execute",
            label="Execute dispatches",
        ),
        WorkflowStage(
            id="experimental_view_refresh",
            label="View refresh",
            optional=True,
            branch_condition="invocation > 1 or last_call",
        ),
    ],
    recurses_into=["experimental", "claim_investigation"],
    notes=[
        "If the prompt file isn't present, the lint test will flag the "
        "stage as referencing a missing prompt; the spec entry can be "
        "amended without code changes.",
    ],
)


_GLOBAL_PRIO_PROFILE = WorkflowProfile(
    name="global_prio",
    kind="orchestrator",
    summary=(
        "Workspace-wide prioritization wrapper — picks which root "
        "question(s) to invest budget in, then delegates each pick to a "
        "per-question orchestrator (typically two_phase)."
    ),
    code_paths=[
        "src/rumil/orchestrators/global_prio.py",
        "src/rumil/orchestrators/base.py",
    ],
    relevant_settings=[
        "enable_global_prio",
        "available_moves",
        "available_calls",
        "prioritizer_variant",
    ],
    stages=[
        WorkflowStage(
            id="global_prio_planning",
            label="Global prioritization",
            description=("Reads all open root questions and emits a budgeted plan across them."),
            prompt_files=["global_prio.md"],
            available_dispatch_call_types=[],
        ),
        WorkflowStage(
            id="global_prio_dispatch",
            label="Per-question delegation",
            description=(
                "For each scheduled root, fires the chosen "
                "per-question orchestrator (driven by "
                "settings.prioritizer_variant)."
            ),
            recurses_into=["two_phase", "experimental"],
        ),
    ],
    recurses_into=["two_phase", "experimental"],
)


_DRAFT_AND_EDIT_PROFILE = WorkflowProfile(
    name="draft_and_edit",
    kind="versus_workflow",
    summary=(
        "SDK-driven essay-completion workflow. Per outer round: the "
        "drafter (or editor on rounds > 1) writes a continuation, then N "
        "critics review in parallel; an optional arbiter merges critic "
        "feedback before the editor revises. Optional planner / brief-audit "
        "/ scout-pass stages augment with anchor research."
    ),
    code_paths=["src/rumil/orchestrators/draft_and_edit.py"],
    relevant_settings=[],
    stages=[
        WorkflowStage(
            id="dae_scout_pass",
            label="Scout pass",
            description=(
                "Optional fast scout pass that surfaces candidate paradigm "
                "cases and hypotheses for the planner to anchor on."
            ),
            optional=True,
            branch_condition="with_scout_pass",
        ),
        WorkflowStage(
            id="dae_planner",
            label="Planner",
            description=(
                "Optional planner that emits a brief (spine + voice + "
                "mandatory anchors) before drafting begins."
            ),
            optional=True,
            branch_condition="with_planner",
        ),
        WorkflowStage(
            id="dae_round_loop",
            label="Round loop",
            description=(
                "Repeats up to max_rounds. Each round consumes 1 budget "
                "unit and ends with a revised draft."
            ),
            loop=True,
        ),
        WorkflowStage(
            id="dae_draft",
            label="Drafter / editor",
            description=(
                "Round 1: drafter writes the initial continuation. "
                "Subsequent rounds: editor revises against the prior draft "
                "+ critique stack + (optional) arbiter focus."
            ),
        ),
        WorkflowStage(
            id="dae_critique",
            label="Critics",
            description=(
                "N parallel critic calls review the current draft for "
                "argument weaknesses, voice drift, length issues."
            ),
        ),
        WorkflowStage(
            id="dae_arbiter",
            label="Arbiter",
            description=(
                "Optional pass that consolidates critic feedback into a "
                "structured accept/reject + focus directive for the editor."
            ),
            optional=True,
            branch_condition="with_arbiter",
        ),
        WorkflowStage(
            id="dae_brief_audit",
            label="Brief audit",
            description=(
                "Optional descriptive audit of the draft as it actually "
                "is, vs the original brief — surfaces drift for the late "
                "rounds."
            ),
            optional=True,
            branch_condition="with_brief_audit",
        ),
    ],
    fingerprint_keys=[
        "n_critics",
        "max_rounds",
        "drafter_model",
        "critic_model",
        "editor_model",
        "with_planner",
        "with_arbiter",
        "with_brief_audit",
        "with_scout_pass",
        "drafter_prompt_hash",
        "critic_prompt_hash",
        "editor_prompt_hash",
    ],
    notes=[
        "Intermediates (drafts, critiques, edits) live as trace events on "
        "the workflow's call, not as workspace pages — keeps essay text "
        "out of embedding search and avoids polluting blind judging.",
        "Per-role models can differ via constructor kwargs; per-role "
        "prompt overrides via *_prompt_path arguments.",
    ],
)


_REFLECTIVE_JUDGE_PROFILE = WorkflowProfile(
    name="reflective_judge",
    kind="versus_workflow",
    summary=(
        "Versus judge workflow that uses a small reflect → read → verdict "
        "loop instead of a single blind LLM call. Stores the verdict as "
        "the question's content for the closer to read."
    ),
    code_paths=["src/rumil/orchestrators/reflective_judge.py"],
    stages=[
        WorkflowStage(
            id="rj_read",
            label="Read pair",
            description="Load the pair surface (essay prefix + both continuations).",
        ),
        WorkflowStage(
            id="rj_reflect",
            label="Reflect",
            description=(
                "Free-form reflection on the relative merits of the two "
                "continuations along the dimension being judged."
            ),
        ),
        WorkflowStage(
            id="rj_verdict",
            label="Verdict",
            description=(
                "Structured verdict — winner + reasoning. Written to the "
                "question's content for the closer."
            ),
        ),
    ],
)


_TWO_PHASE_VERSUS_PROFILE = WorkflowProfile(
    name="two_phase_versus",
    kind="versus_workflow",
    summary=(
        "Versus workflow wrapping the standard TwoPhaseOrchestrator. A "
        "research run on the pair-surface question; the closer extracts "
        "the verdict from the resulting view."
    ),
    code_paths=["src/rumil/versus_workflow.py"],
    relevant_settings=[
        "assess_call_variant",
        "available_moves",
        "available_calls",
        "enable_red_team",
        "enable_global_prio",
        "subquestion_linker_enabled",
        "prioritizer_variant",
        "view_variant",
        "budget_pacing_enabled",
    ],
    stages=[
        WorkflowStage(
            id="tpv_setup",
            label="Setup",
            description="Seed the budget on the question's DB.",
        ),
        WorkflowStage(
            id="tpv_two_phase",
            label="TwoPhaseOrchestrator",
            description="Delegate to the standard TwoPhaseOrchestrator on the pair-surface question.",
            recurses_into=["two_phase"],
        ),
        WorkflowStage(
            id="tpv_closer",
            label="Closer",
            description=(
                "Extract verdict from the orchestrator's output — reads "
                "the closing view rather than the question content."
            ),
        ),
    ],
    recurses_into=["two_phase"],
    fingerprint_keys=["budget", "settings.*"],
)


_PROFILES: dict[str, WorkflowProfile] = {
    p.name: p
    for p in [
        _TWO_PHASE_PROFILE,
        _CLAIM_INVESTIGATION_PROFILE,
        _EXPERIMENTAL_PROFILE,
        _GLOBAL_PRIO_PROFILE,
        _DRAFT_AND_EDIT_PROFILE,
        _REFLECTIVE_JUDGE_PROFILE,
        _TWO_PHASE_VERSUS_PROFILE,
    ]
}


def list_workflow_summaries() -> list[WorkflowSummary]:
    return [
        WorkflowSummary(
            name=p.name,
            kind=p.kind,
            summary=p.summary,
            code_paths=list(p.code_paths),
        )
        for p in _PROFILES.values()
    ]


def get_workflow_profile(name: str) -> WorkflowProfile | None:
    return _PROFILES.get(name)


def all_profiles() -> list[WorkflowProfile]:
    return list(_PROFILES.values())
