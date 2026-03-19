"""Call types for the research workspace."""

from rumil.calls.assess import AssessCall, EmbeddingAssessCall
from rumil.calls.base import BaseCall, SimpleCall
from rumil.calls.call_registry import (
    ASSESS_CALL_CLASSES,
    INGEST_CALL_CLASSES,
    SCOUT_ANALOGIES_CALL_CLASSES,
    SCOUT_CALL_CLASSES,
    WEB_RESEARCH_CALL_CLASSES,
    SCOUT_ESTIMATES_CALL_CLASSES,
    SCOUT_HYPOTHESES_CALL_CLASSES,
    SCOUT_SUBQUESTIONS_CALL_CLASSES,
)
from rumil.calls.ingest import EmbeddingIngestCall, IngestCall
from rumil.calls.prioritization import run_prioritization
from rumil.calls.scout import EmbeddingScoutCall, ScoutCall
from rumil.calls.web_research import WebResearchCall
from rumil.calls.scout_analogies import ScoutAnalogiesCall
from rumil.calls.scout_estimates import ScoutEstimatesCall
from rumil.calls.scout_hypotheses import ScoutHypothesesCall
from rumil.calls.scout_subquestions import ScoutSubquestionsCall

__all__ = [
    "BaseCall",
    "SimpleCall",
    "AssessCall",
    "EmbeddingAssessCall",
    "IngestCall",
    "EmbeddingIngestCall",
    "ScoutCall",
    "EmbeddingScoutCall",
    "ScoutSubquestionsCall",
    "ScoutEstimatesCall",
    "ScoutHypothesesCall",
    "ScoutAnalogiesCall",
    "SCOUT_CALL_CLASSES",
    "ASSESS_CALL_CLASSES",
    "INGEST_CALL_CLASSES",
    "WEB_RESEARCH_CALL_CLASSES",
    "WebResearchCall",
    "SCOUT_SUBQUESTIONS_CALL_CLASSES",
    "SCOUT_ESTIMATES_CALL_CLASSES",
    "SCOUT_HYPOTHESES_CALL_CLASSES",
    "SCOUT_ANALOGIES_CALL_CLASSES",
    "run_prioritization",
]
