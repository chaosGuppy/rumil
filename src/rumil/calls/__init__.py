"""Call types for the research workspace."""

from rumil.calls.assess import AssessCall, EmbeddingAssessCall
from rumil.calls.call_registry import (
    ASSESS_CALL_CLASSES,
    INGEST_CALL_CLASSES,
    SCOUT_ANALOGIES_CALL_CLASSES,
    SCOUT_FACTCHECKS_CALL_CLASSES,
    SCOUT_PARADIGM_CASES_CALL_CLASSES,
    SCOUT_DEEP_QUESTIONS_CALL_CLASSES,
    SCOUT_WEB_QUESTIONS_CALL_CLASSES,
    FIND_CONSIDERATIONS_CALL_CLASSES,
    WEB_RESEARCH_CALL_CLASSES,
    SCOUT_ESTIMATES_CALL_CLASSES,
    SCOUT_HYPOTHESES_CALL_CLASSES,
    SCOUT_SUBQUESTIONS_CALL_CLASSES,
)
from rumil.calls.ingest import EmbeddingIngestCall, IngestCall
from rumil.calls.prioritization import run_prioritization
from rumil.calls.find_considerations import (
    EmbeddingFindConsiderationsCall,
    FindConsiderationsCall,
)
from rumil.calls.stages import CallRunner
from rumil.calls.web_research import WebResearchCall
from rumil.calls.scout_analogies import ScoutAnalogiesCall
from rumil.calls.scout_paradigm_cases import ScoutParadigmCasesCall
from rumil.calls.scout_estimates import ScoutEstimatesCall
from rumil.calls.scout_hypotheses import ScoutHypothesesCall
from rumil.calls.scout_factchecks import ScoutFactchecksCall
from rumil.calls.scout_subquestions import ScoutSubquestionsCall
from rumil.calls.scout_deep_questions import ScoutDeepQuestionsCall
from rumil.calls.scout_web_questions import ScoutWebQuestionsCall

__all__ = [
    "CallRunner",
    "AssessCall",
    "EmbeddingAssessCall",
    "IngestCall",
    "EmbeddingIngestCall",
    "FindConsiderationsCall",
    "EmbeddingFindConsiderationsCall",
    "ScoutSubquestionsCall",
    "ScoutEstimatesCall",
    "ScoutHypothesesCall",
    "ScoutAnalogiesCall",
    "ScoutParadigmCasesCall",
    "FIND_CONSIDERATIONS_CALL_CLASSES",
    "ASSESS_CALL_CLASSES",
    "INGEST_CALL_CLASSES",
    "WEB_RESEARCH_CALL_CLASSES",
    "WebResearchCall",
    "SCOUT_SUBQUESTIONS_CALL_CLASSES",
    "SCOUT_ESTIMATES_CALL_CLASSES",
    "SCOUT_HYPOTHESES_CALL_CLASSES",
    "SCOUT_ANALOGIES_CALL_CLASSES",
    "SCOUT_FACTCHECKS_CALL_CLASSES",
    "SCOUT_PARADIGM_CASES_CALL_CLASSES",
    "ScoutFactchecksCall",
    "ScoutWebQuestionsCall",
    "SCOUT_WEB_QUESTIONS_CALL_CLASSES",
    "ScoutDeepQuestionsCall",
    "SCOUT_DEEP_QUESTIONS_CALL_CLASSES",
    "run_prioritization",
]
