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
    score_items_sequentially,
    find_considerations_until_done,
    ingest_until_done,
    web_research_question,
)
from rumil.orchestrators.experimental import ExperimentalOrchestrator
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster


def Orchestrator(db: DB, broadcaster: Broadcaster | None = None) -> BaseOrchestrator:
    """Factory function: returns the appropriate orchestrator subclass."""
    variant = get_settings().prioritizer_variant
    if variant == "two_phase":
        return TwoPhaseOrchestrator(db, broadcaster)
    if variant == "experimental":
        return ExperimentalOrchestrator(db, broadcaster)
    raise ValueError(f"Unknown prioritizer_variant: {variant}")


__all__ = [
    "BaseOrchestrator",
    "ClaimInvestigationOrchestrator",
    "ClaimScore",
    "ClaimScoringResult",
    "ExperimentalOrchestrator",
    "FruitResult",
    "Orchestrator",
    "PRIORITIZATION_MOVES",
    "PrioritizationResult",
    "SubquestionScore",
    "SubquestionScoringResult",
    "TwoPhaseOrchestrator",
    "assess_question",
    "compute_priority_score",
    "create_root_question",
    "find_considerations_until_done",
    "score_items_sequentially",
    "ingest_until_done",
    "web_research_question",
]
