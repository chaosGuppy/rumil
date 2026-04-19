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
from dataclasses import dataclass
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
from rumil.orchestrators.policy_layer import PolicyOrchestrator
from rumil.orchestrators.refine_artifact import RefineArtifactOrchestrator
from rumil.orchestrators.source_first import SourceFirstOrchestrator
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator
from rumil.tracing.broadcast import Broadcaster

Runnable = Any


@dataclass(frozen=True)
class OrchestratorSpec:
    variant: str
    description: str
    stability: str
    cost_band: str
    factory: Callable[[DB, Broadcaster | None], Runnable]
    exposed_in_chat: bool = True
    supports_global_prio: bool = True
    supported_call_types: Sequence[CallType] | None = None


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
