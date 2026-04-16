"""
Orchestrators: drive the research workflow using the prioritization system.
Budget is tracked here; prioritization and review calls are free.
"""

from rumil.database import DB
from rumil.orchestrators.base import BaseOrchestrator
from rumil.orchestrators.claim_investigation import ClaimInvestigationOrchestrator
from rumil.orchestrators.common import (
    ClaimScore,
    ClaimScoringResult,
    FruitResult,
    PRIORITIZATION_MOVES,
    PrioritizationResult,
    SubquestionScore,
    SubquestionScoringResult,
    assess_question,
    compute_priority_score,
    create_root_question,
    create_view_for_question,
    score_items_sequentially,
    find_considerations_until_done,
    ingest_until_done,
    update_view_for_question,
    web_research_question,
)
from rumil.orchestrators.experimental import ExperimentalOrchestrator
from rumil.orchestrators.global_prio import GlobalPrioOrchestrator
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster


def Orchestrator(db: DB, broadcaster: Broadcaster | None = None) -> BaseOrchestrator:
    """Factory function: returns the appropriate orchestrator subclass."""
    settings = get_settings()
    variant = settings.prioritizer_variant
    if variant == "two_phase":
        orch: BaseOrchestrator = TwoPhaseOrchestrator(db, broadcaster)
    elif variant == "experimental":
        orch = ExperimentalOrchestrator(db, broadcaster)
    else:
        raise ValueError(f"Unknown prioritizer_variant: {variant}")

    if settings.enable_global_prio:
        return GlobalPrioOrchestrator(db, broadcaster)
    return orch


__all__ = [
    "BaseOrchestrator",
    "ClaimInvestigationOrchestrator",
    "ClaimScore",
    "ClaimScoringResult",
    "ExperimentalOrchestrator",
    "GlobalPrioOrchestrator",
    "FruitResult",
    "Orchestrator",
    "PRIORITIZATION_MOVES",
    "PrioritizationResult",
    "SubquestionScore",
    "SubquestionScoringResult",
    "TwoPhaseOrchestrator",
    "assess_question",
    "compute_priority_score",
    "create_view_for_question",
    "create_root_question",
    "find_considerations_until_done",
    "update_view_for_question",
    "score_items_sequentially",
    "ingest_until_done",
    "web_research_question",
]
