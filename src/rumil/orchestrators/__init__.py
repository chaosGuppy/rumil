"""
Orchestrators: drive the research workflow using the prioritization system.
Budget is tracked here; prioritization and review calls are free.
"""

from rumil.database import DB
from rumil.orchestrators.base import BaseOrchestrator
from rumil.orchestrators.claim_investigation import ClaimInvestigationOrchestrator
from rumil.orchestrators.common import (
    PRIORITIZATION_MOVES,
    ClaimScore,
    ClaimScoringResult,
    FruitResult,
    PrioritizationResult,
    SubquestionScore,
    SubquestionScoringResult,
    assess_question,
    compute_priority_score,
    count_sources_for_question,
    create_root_question,
    create_view_for_question,
    find_considerations_until_done,
    ingest_until_done,
    score_items_sequentially,
    update_view_for_question,
    web_research_question,
)
from rumil.orchestrators.critique_first import CritiqueFirstOrchestrator
from rumil.orchestrators.experimental import ExperimentalOrchestrator
from rumil.orchestrators.global_prio import GlobalPrioOrchestrator
from rumil.orchestrators.policies import (
    cascade_policies,
    distill_first_policies,
    worldview_policies,
)
from rumil.orchestrators.policy_layer import PolicyOrchestrator
from rumil.orchestrators.refine_artifact import (
    RefineArtifactOrchestrator,
    RefineArtifactResult,
    RefineIteration,
)
from rumil.orchestrators.registry import (
    ORCHESTRATORS,
    OrchestratorSpec,
    build_orchestrator,
    get_orchestrator_spec,
)
from rumil.orchestrators.source_first import SourceFirstOrchestrator
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator
from rumil.tracing.broadcast import Broadcaster


def Orchestrator(db: DB, broadcaster: Broadcaster | None = None):
    """Factory function: returns an orchestrator from the registry.

    Reads ``settings.prioritizer_variant`` and ``settings.enable_global_prio``.
    Returns ``BaseOrchestrator`` for most variants, or
    ``RefineArtifactOrchestrator`` when the variant is ``refine_artifact``.
    """
    return build_orchestrator(db, broadcaster)


__all__ = [
    "ORCHESTRATORS",
    "PRIORITIZATION_MOVES",
    "BaseOrchestrator",
    "ClaimInvestigationOrchestrator",
    "ClaimScore",
    "ClaimScoringResult",
    "CritiqueFirstOrchestrator",
    "ExperimentalOrchestrator",
    "FruitResult",
    "GlobalPrioOrchestrator",
    "Orchestrator",
    "OrchestratorSpec",
    "PolicyOrchestrator",
    "PrioritizationResult",
    "RefineArtifactOrchestrator",
    "RefineArtifactResult",
    "RefineIteration",
    "SubquestionScore",
    "SubquestionScoringResult",
    "TwoPhaseOrchestrator",
    "assess_question",
    "build_orchestrator",
    "compute_priority_score",
    "create_root_question",
    "create_view_for_question",
    "distill_first_policies",
    "find_considerations_until_done",
    "get_orchestrator_spec",
    "ingest_until_done",
    "score_items_sequentially",
    "update_view_for_question",
    "web_research_question",
    "worldview_policies",
]
