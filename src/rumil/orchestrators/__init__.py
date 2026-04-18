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
from rumil.orchestrators.distill_first import DistillFirstOrchestrator
from rumil.orchestrators.experimental import ExperimentalOrchestrator
from rumil.orchestrators.global_prio import GlobalPrioOrchestrator
from rumil.orchestrators.source_first import SourceFirstOrchestrator
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator
from rumil.orchestrators.worldview import WorldviewOrchestrator
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
    elif variant == "worldview":
        orch = WorldviewOrchestrator(db, broadcaster)
    elif variant == "distill_first":
        orch = DistillFirstOrchestrator(db, broadcaster)
    elif variant == "critique_first":
        orch = CritiqueFirstOrchestrator(db, broadcaster)
    else:
        raise ValueError(f"Unknown prioritizer_variant: {variant}")

    if settings.enable_global_prio:
        return GlobalPrioOrchestrator(db, broadcaster)
    return orch


__all__ = [
    "PRIORITIZATION_MOVES",
    "BaseOrchestrator",
    "ClaimInvestigationOrchestrator",
    "ClaimScore",
    "ClaimScoringResult",
    "CritiqueFirstOrchestrator",
    "DistillFirstOrchestrator",
    "ExperimentalOrchestrator",
    "FruitResult",
    "GlobalPrioOrchestrator",
    "Orchestrator",
    "PrioritizationResult",
    "SubquestionScore",
    "SubquestionScoringResult",
    "TwoPhaseOrchestrator",
    "WorldviewOrchestrator",
    "assess_question",
    "compute_priority_score",
    "create_root_question",
    "create_view_for_question",
    "find_considerations_until_done",
    "ingest_until_done",
    "score_items_sequentially",
    "update_view_for_question",
    "web_research_question",
]
