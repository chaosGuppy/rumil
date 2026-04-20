"""Orchestrator registry.

Single source of truth for variant → orchestrator class + metadata. The CLI,
API, chat, and skills all iterate this registry instead of maintaining their
own if/elif chains.

To add a new orchestrator: write the class, register it here, done. Every
surface that reads the registry (the ``Orchestrator()`` factory in this
package, ``dispatch_orchestrator`` in ``rumil.dispatch``, chat's
``orchestrate`` tool catalog, the ``/api/capabilities`` endpoint) picks it
up automatically.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from rumil.database import DB
from rumil.models import CallType
from rumil.orchestrators.base import BaseOrchestrator
from rumil.orchestrators.critique_first import CritiqueFirstOrchestrator
from rumil.orchestrators.experimental import ExperimentalOrchestrator
from rumil.orchestrators.global_prio import GlobalPrioOrchestrator
from rumil.orchestrators.policies import (
    cascade_policies,
    distill_first_policies,
    worldview_policies,
)
from rumil.orchestrators.policy_layer import Policy, PolicyOrchestrator
from rumil.orchestrators.refine_artifact import RefineArtifactOrchestrator
from rumil.orchestrators.source_first import SourceFirstOrchestrator
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator
from rumil.tracing.broadcast import Broadcaster

Runnable = Any

PolicyFactory = Callable[[DB], Sequence[Policy]]


@dataclass(frozen=True)
class OrchestratorSpec:
    """Describes one orchestrator variant.

    Fields split by what they're used for:
      * ``factory`` / ``variant`` / ``stability`` / ``cost_band`` /
        ``description`` / ``exposed_in_chat`` / ``supports_global_prio``
        are the runtime-and-picker surface.
      * ``overview`` / ``diagram_mermaid`` / ``static_phases`` /
        ``policy_factory`` / ``related_call_types`` are the "how it
        works" surface read by the UI's orchestrator info popover.

    For ``PolicyOrchestrator``-backed variants, set ``policy_factory``;
    the info endpoint will call it and derive the phase list from the
    live ``Policy.name`` / ``Policy.description`` attributes, so the docs
    cannot drift from the code. For hand-coded orchestrators, fill
    ``static_phases`` with a short ordered list of step names. Either
    ``policy_factory`` or ``static_phases`` (or neither) is fine.
    """

    variant: str
    description: str
    stability: str
    cost_band: str
    factory: Callable[[DB, Broadcaster | None], Runnable]
    exposed_in_chat: bool = True
    supports_global_prio: bool = True
    supported_call_types: Sequence[CallType] | None = None
    overview: str = ""
    diagram_mermaid: str = ""
    static_phases: Sequence[str] = field(default_factory=tuple)
    policy_factory: PolicyFactory | None = None
    related_call_types: Sequence[CallType] = field(default_factory=tuple)


def _two_phase(db: DB, broadcaster: Broadcaster | None) -> BaseOrchestrator:
    return TwoPhaseOrchestrator(db, broadcaster)


def _experimental(db: DB, broadcaster: Broadcaster | None) -> BaseOrchestrator:
    return ExperimentalOrchestrator(db, broadcaster)


def _worldview(db: DB, broadcaster: Broadcaster | None) -> BaseOrchestrator:
    return PolicyOrchestrator(db, worldview_policies(db), broadcaster)


def _distill_first(db: DB, broadcaster: Broadcaster | None) -> BaseOrchestrator:
    return PolicyOrchestrator(db, distill_first_policies(), broadcaster)


def _critique_first(db: DB, broadcaster: Broadcaster | None) -> BaseOrchestrator:
    return CritiqueFirstOrchestrator(db, broadcaster)


def _cascade(db: DB, broadcaster: Broadcaster | None) -> BaseOrchestrator:
    return PolicyOrchestrator(db, cascade_policies(db), broadcaster)


def _source_first(db: DB, broadcaster: Broadcaster | None) -> BaseOrchestrator:
    return SourceFirstOrchestrator(db, broadcaster)


def _refine_artifact(db: DB, broadcaster: Broadcaster | None) -> RefineArtifactOrchestrator:
    return RefineArtifactOrchestrator(db, broadcaster)


def _distill_first_policies(db: DB) -> Sequence[Policy]:
    return distill_first_policies()


_WORLDVIEW_DIAGRAM = (
    "flowchart TD\n"
    "  A[capture state] --> B{budget > 0?}\n"
    "  B -- no --> T[terminate]\n"
    "  B -- yes --> C{cascade in scope?}\n"
    "  C -- yes --> D[dispatch assess]\n"
    "  C -- no --> E{missing credence or\\nunjudged children?}\n"
    "  E -- yes --> F[dispatch assess]\n"
    "  E -- no --> G{page count < 3?}\n"
    "  G -- yes --> H[dispatch find_considerations]\n"
    "  G -- no --> T\n"
)

_DISTILL_FIRST_DIAGRAM = (
    "flowchart TD\n"
    "  A[capture state] --> B{budget > 0?}\n"
    "  B -- no --> T[terminate]\n"
    "  B -- yes --> C{view exists?}\n"
    "  C -- no --> D[dispatch create_view]\n"
    "  C -- yes --> E{last call was a mutation?}\n"
    "  E -- yes --> F[dispatch update_view]\n"
    "  E -- no --> G{gaps in view?}\n"
    "  G -- yes --> H[dispatch assess on gap]\n"
    "  G -- no --> T\n"
)

_CASCADE_DIAGRAM = (
    "flowchart TD\n"
    "  A[capture state] --> B{budget > 0?}\n"
    "  B -- no --> T[terminate]\n"
    "  B -- yes --> C{pending cascade?}\n"
    "  C -- yes --> D[dispatch assess on target]\n"
    "  C -- no --> T\n"
)

_TWO_PHASE_DIAGRAM = (
    "flowchart TD\n"
    "  A[initial prioritization call] --> B[fan-out: scouts + find_considerations]\n"
    "  B --> C[main-phase prioritization call]\n"
    "  C --> D[dispatch batch: scouts / assess / find / web_research]\n"
    "  D --> E{budget remaining?}\n"
    "  E -- yes --> C\n"
    "  E -- no --> T[terminate]\n"
)

_CRITIQUE_FIRST_DIAGRAM = (
    "flowchart TD\n"
    "  A[prioritization picks a question] --> B[scout_how_true]\n"
    "  B --> C[scout_how_false]\n"
    "  C --> D[find_considerations]\n"
    "  D --> E[assess]\n"
    "  E --> F{budget remaining?}\n"
    "  F -- yes --> A\n"
    "  F -- no --> T[terminate]\n"
)

_SOURCE_FIRST_DIAGRAM = (
    "flowchart TD\n"
    "  A[iteration start] --> B{has uningested sources?}\n"
    "  B -- yes --> C[ingest source]\n"
    "  B -- no --> D[web_research]\n"
    "  C --> E[find_considerations]\n"
    "  D --> E\n"
    "  E --> F[assess]\n"
    "  F --> G{budget remaining?}\n"
    "  G -- yes --> A\n"
    "  G -- no --> T[terminate]\n"
)

_REFINE_ARTIFACT_DIAGRAM = (
    "flowchart TD\n"
    "  A[draft artifact] --> B[adversarial review]\n"
    "  B --> C[refine artifact]\n"
    "  C --> D{rounds remaining?}\n"
    "  D -- yes --> B\n"
    "  D -- no --> T[final artifact]\n"
)

_EXPERIMENTAL_DIAGRAM = _TWO_PHASE_DIAGRAM


ORCHESTRATORS: dict[str, OrchestratorSpec] = {
    "two_phase": OrchestratorSpec(
        variant="two_phase",
        description=(
            "Default. Two-phase breadth-then-depth: score and expand subquestions "
            "broadly, then deepen targeted claims. Good general-purpose choice."
        ),
        stability="stable",
        cost_band="medium",
        factory=_two_phase,
        overview=(
            "An initial prioritization call fans out scouts and find_considerations "
            "to build breadth on the root question. Subsequent main-phase "
            "prioritization calls batch-dispatch the next set of scouts, "
            "find_considerations, assesses, and web_research until budget runs out."
        ),
        static_phases=(
            "Initial prioritization — score and expand the root question.",
            "Main-phase prioritization — pick the next batch of calls.",
            "Dispatch batch — scouts / find_considerations / assess / web_research.",
            "Loop until budget exhausted.",
        ),
        diagram_mermaid=_TWO_PHASE_DIAGRAM,
        related_call_types=(
            CallType.FIND_CONSIDERATIONS,
            CallType.ASSESS,
            CallType.WEB_RESEARCH,
            CallType.SCOUT_SUBQUESTIONS,
            CallType.SCOUT_ESTIMATES,
            CallType.SCOUT_HYPOTHESES,
        ),
    ),
    "experimental": OrchestratorSpec(
        variant="experimental",
        description=(
            "Experimental variant for in-flight strategies. Currently mirrors "
            "two_phase — use when trying out changes to the prioritization loop."
        ),
        stability="experimental",
        cost_band="medium",
        factory=_experimental,
        overview=(
            "Staging ground for prioritization-loop changes. Whatever "
            "experiment is currently in flight lives here. Right now it "
            "mirrors two_phase."
        ),
        static_phases=("Whatever two_phase does, plus the current experiment's overlay.",),
        diagram_mermaid=_EXPERIMENTAL_DIAGRAM,
    ),
    "worldview": OrchestratorSpec(
        variant="worldview",
        description=(
            "Cycles explore/evaluate modes and drains CASCADE_REVIEW suggestions. "
            "Best for questions where you want to alternate discovery and review."
        ),
        stability="stable",
        cost_band="medium",
        factory=_worldview,
        overview=(
            "A PolicyOrchestrator. Each iteration snapshots the question's state, "
            "then checks policies in order: budget, in-scope cascade reviews, "
            "view health, and a sparse-question fallback that explores when "
            "page count is low. The phase list below is derived directly from "
            "the live ``worldview_policies()`` composition."
        ),
        policy_factory=worldview_policies,
        diagram_mermaid=_WORLDVIEW_DIAGRAM,
        related_call_types=(CallType.FIND_CONSIDERATIONS, CallType.ASSESS),
    ),
    "distill_first": OrchestratorSpec(
        variant="distill_first",
        description=(
            "View-centric: creates/updates the distillation view early, then fills "
            "credence/importance gaps for top-ranked claims. Best when you want a "
            "crisp summary rather than broad exploration."
        ),
        stability="stable",
        cost_band="medium",
        factory=_distill_first,
        overview=(
            "A PolicyOrchestrator. Seeds a distillation view on the first "
            "iteration, refreshes it after any mutation, and fills credence / "
            "importance gaps surfaced by view health. The phase list below is "
            "derived directly from the live ``distill_first_policies()`` "
            "composition."
        ),
        policy_factory=_distill_first_policies,
        diagram_mermaid=_DISTILL_FIRST_DIAGRAM,
        related_call_types=(
            CallType.ASSESS,
            CallType.CREATE_VIEW,
            CallType.UPDATE_VIEW,
        ),
    ),
    "critique_first": OrchestratorSpec(
        variant="critique_first",
        description=(
            "Adversarial framing: how-true/how-false scouts run before "
            "find_considerations. Best when you want the workspace to stress-test "
            "claims early."
        ),
        stability="stable",
        cost_band="medium",
        factory=_critique_first,
        overview=(
            "A hand-coded orchestrator. Each iteration picks a question via "
            "prioritization, fires scout_how_true and scout_how_false before "
            "find_considerations so the substrate sees adversarial framings "
            "up front, then runs assess."
        ),
        static_phases=(
            "Prioritization picks the question.",
            "scout_how_true — what would make this true.",
            "scout_how_false — what would make this false.",
            "find_considerations — general expansion.",
            "assess — rate credence/robustness.",
            "Loop until budget exhausted.",
        ),
        diagram_mermaid=_CRITIQUE_FIRST_DIAGRAM,
        related_call_types=(
            CallType.SCOUT_C_HOW_TRUE,
            CallType.SCOUT_C_HOW_FALSE,
            CallType.FIND_CONSIDERATIONS,
            CallType.ASSESS,
        ),
    ),
    "cascade": OrchestratorSpec(
        variant="cascade",
        description=(
            "Reputation-loop driver: drains pending CASCADE_REVIEW suggestions by "
            "running targeted assessments. Best as a follow-up when a question has "
            "lots of pending cascade work."
        ),
        stability="stable",
        cost_band="low",
        factory=_cascade,
        overview=(
            "A PolicyOrchestrator with a narrow job: drain the "
            "CASCADE_REVIEW suggestion queue by dispatching assess on each "
            "target, then terminate when the queue is empty. The phase list "
            "below is derived from the live ``cascade_policies()`` "
            "composition."
        ),
        policy_factory=cascade_policies,
        diagram_mermaid=_CASCADE_DIAGRAM,
        related_call_types=(CallType.ASSESS,),
    ),
    "source_first": OrchestratorSpec(
        variant="source_first",
        description=(
            "Web research or source ingest runs before find_considerations each "
            "iteration. Best when source-grounded discovery matters more than "
            "pure LLM expansion."
        ),
        stability="stable",
        cost_band="high",
        factory=_source_first,
        overview=(
            "A hand-coded orchestrator. Each iteration prefers ingesting "
            "already-attached sources; when none remain, it runs web_research "
            "to pull in new sources. find_considerations and assess follow "
            "the source step so new pages get grounded and rated."
        ),
        static_phases=(
            "Ingest step — if uningested sources exist, ingest one; else web_research.",
            "find_considerations — expand on the freshly-grounded material.",
            "assess — rate credence/robustness.",
            "Loop until budget exhausted.",
        ),
        diagram_mermaid=_SOURCE_FIRST_DIAGRAM,
        related_call_types=(
            CallType.INGEST,
            CallType.WEB_RESEARCH,
            CallType.FIND_CONSIDERATIONS,
            CallType.ASSESS,
        ),
    ),
    "refine_artifact": OrchestratorSpec(
        variant="refine_artifact",
        description=(
            "Draft → adversarial review → refine loop. Not a prioritization "
            "orchestrator — composes DraftArtifactCall + AdversarialReviewCall "
            "in tight iterations. CLI-only for now (different run shape)."
        ),
        stability="cli_only",
        cost_band="medium",
        factory=_refine_artifact,
        exposed_in_chat=False,
        supports_global_prio=False,
        overview=(
            "Shape is different from other orchestrators: no prioritization, "
            "no page graph, just a draft / review / refine loop over an "
            "artifact page until the reviewer stops finding issues or the "
            "round cap is hit. CLI-only today."
        ),
        static_phases=(
            "Draft the artifact.",
            "Adversarial review — find weaknesses.",
            "Refine the artifact in response.",
            "Loop until reviewer is satisfied or rounds exhausted.",
        ),
        diagram_mermaid=_REFINE_ARTIFACT_DIAGRAM,
    ),
}


def get_orchestrator_spec(variant: str) -> OrchestratorSpec:
    spec = ORCHESTRATORS.get(variant)
    if spec is None:
        raise ValueError(
            f"Unknown prioritizer_variant: {variant!r}. Available: {sorted(ORCHESTRATORS)}"
        )
    return spec


def build_orchestrator(
    db: DB,
    broadcaster: Broadcaster | None = None,
    *,
    variant: str | None = None,
    enable_global_prio: bool | None = None,
) -> Runnable:
    """Build an orchestrator from the registry.

    Reads ``settings.prioritizer_variant`` and ``settings.enable_global_prio``
    when ``variant`` or ``enable_global_prio`` are not supplied.

    ``enable_global_prio`` wraps the selected orchestrator in
    ``GlobalPrioOrchestrator`` (preserving existing semantics: the wrapper
    fully replaces the selected orchestrator's behaviour). Variants marked
    ``supports_global_prio=False`` (``refine_artifact``) are returned directly.
    """
    from rumil.settings import get_settings

    settings = get_settings()
    v = variant or settings.prioritizer_variant
    use_global_prio = (
        settings.enable_global_prio if enable_global_prio is None else enable_global_prio
    )

    spec = get_orchestrator_spec(v)
    if not spec.supports_global_prio:
        return spec.factory(db, broadcaster)
    if use_global_prio:
        return GlobalPrioOrchestrator(db, broadcaster)
    return spec.factory(db, broadcaster)
