"""
Orchestrators: drive the research workflow using the prioritization system.
Budget is tracked here; prioritization and review calls are free.
"""

from rumil.database import DB
from rumil.orchestrators.base import BaseOrchestrator
from rumil.orchestrators.common import (
    CallTypeFruitScore,
    FruitResult,
    PRIORITIZATION_MOVES,
    PerTypeFruitResult,
    PrioritizationResult,
    SubquestionScore,
    SubquestionScoringResult,
    assess_question,
    compute_dispatch_guidance,
    create_root_question,
    find_considerations_until_done,
    ingest_until_done,
    run_concept_session,
    web_research_question,
)
from rumil.orchestrators.llm import LLMOrchestrator
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster


def Orchestrator(db: DB, broadcaster: Broadcaster | None = None) -> BaseOrchestrator:
    """Factory function: returns the appropriate orchestrator subclass."""
    if get_settings().prioritizer_variant == "two_phase":
        return TwoPhaseOrchestrator(db, broadcaster)
    return LLMOrchestrator(db, broadcaster)


__all__ = [
    "BaseOrchestrator",
    "CallTypeFruitScore",
    "FruitResult",
    "LLMOrchestrator",
    "Orchestrator",
    "PRIORITIZATION_MOVES",
    "PerTypeFruitResult",
    "PrioritizationResult",
    "SubquestionScore",
    "SubquestionScoringResult",
    "TwoPhaseOrchestrator",
    "assess_question",
    "compute_dispatch_guidance",
    "create_root_question",
    "find_considerations_until_done",
    "ingest_until_done",
    "run_concept_session",
    "web_research_question",
]
