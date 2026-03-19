"""Call types for the research workspace."""

from rumil.calls.assess import AssessCall, EmbeddingAssessCall
from rumil.calls.base import BaseCall, SimpleCall
from rumil.calls.call_registry import (
    ASSESS_CALL_CLASSES,
    INGEST_CALL_CLASSES,
    SCOUT_CALL_CLASSES,
    WEB_RESEARCH_CALL_CLASSES,
)
from rumil.calls.ingest import EmbeddingIngestCall, IngestCall
from rumil.calls.prioritization import run_prioritization
from rumil.calls.scout import EmbeddingScoutCall, ScoutCall
from rumil.calls.web_research import WebResearchCall

__all__ = [
    "BaseCall",
    "SimpleCall",
    "AssessCall",
    "EmbeddingAssessCall",
    "IngestCall",
    "EmbeddingIngestCall",
    "ScoutCall",
    "EmbeddingScoutCall",
    "SCOUT_CALL_CLASSES",
    "ASSESS_CALL_CLASSES",
    "INGEST_CALL_CLASSES",
    "WEB_RESEARCH_CALL_CLASSES",
    "WebResearchCall",
    "run_prioritization",
]
