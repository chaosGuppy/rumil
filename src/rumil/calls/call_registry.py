"""Registries mapping variant names to call classes.

Each call type (find_considerations, assess, ingest) has its own registry so
callers can look up the concrete class by a short string name stored in settings.
"""

from rumil.calls.assess import AssessCall, BigAssessCall, EmbeddingAssessCall
from rumil.calls.assess_concept import AssessConceptCall
from rumil.calls.ingest import EmbeddingIngestCall, IngestCall
from rumil.calls.find_considerations import (
    EmbeddingFindConsiderationsCall,
    FindConsiderationsCall,
)
from rumil.calls.scout_analogies import ScoutAnalogiesCall
from rumil.calls.scout_factchecks import ScoutFactchecksCall
from rumil.calls.scout_paradigm_cases import ScoutParadigmCasesCall
from rumil.calls.scout_deep_questions import ScoutDeepQuestionsCall
from rumil.calls.scout_web_questions import ScoutWebQuestionsCall
from rumil.calls.scout_c_cruxes import ScoutCCruxesCall
from rumil.calls.scout_c_how_false import ScoutCHowFalseCall
from rumil.calls.scout_c_how_true import ScoutCHowTrueCall
from rumil.calls.scout_c_relevant_evidence import ScoutCRelevantEvidenceCall
from rumil.calls.scout_c_robustify import ScoutCRobustifyCall
from rumil.calls.scout_c_strengthen import ScoutCStrengthenCall
from rumil.calls.scout_c_stress_test_cases import ScoutCStressTestCasesCall
from rumil.calls.scout_concepts import ScoutConceptsCall
from rumil.calls.web_research import WebResearchCall
from rumil.calls.scout_estimates import ScoutEstimatesCall
from rumil.calls.scout_hypotheses import ScoutHypothesesCall
from rumil.calls.scout_subquestions import ScoutSubquestionsCall

FIND_CONSIDERATIONS_CALL_CLASSES: dict[str, type[FindConsiderationsCall]] = {
    "default": FindConsiderationsCall,
    "embedding": EmbeddingFindConsiderationsCall,
}

ASSESS_CALL_CLASSES: dict[str, type[AssessCall]] = {
    "default": AssessCall,
    "embedding": EmbeddingAssessCall,
    "big": BigAssessCall,
}

INGEST_CALL_CLASSES: dict[str, type[IngestCall]] = {
    "default": IngestCall,
    "embedding": EmbeddingIngestCall,
}

SCOUT_CONCEPTS_CALL_CLASSES: dict[str, type[ScoutConceptsCall]] = {
    "default": ScoutConceptsCall,
}

ASSESS_CONCEPT_CALL_CLASSES: dict[str, type[AssessConceptCall]] = {
    "default": AssessConceptCall,
}

WEB_RESEARCH_CALL_CLASSES: dict[str, type[WebResearchCall]] = {
    "default": WebResearchCall,
}

SCOUT_SUBQUESTIONS_CALL_CLASSES: dict[str, type[ScoutSubquestionsCall]] = {
    "default": ScoutSubquestionsCall,
}

SCOUT_ESTIMATES_CALL_CLASSES: dict[str, type[ScoutEstimatesCall]] = {
    "default": ScoutEstimatesCall,
}

SCOUT_HYPOTHESES_CALL_CLASSES: dict[str, type[ScoutHypothesesCall]] = {
    "default": ScoutHypothesesCall,
}

SCOUT_ANALOGIES_CALL_CLASSES: dict[str, type[ScoutAnalogiesCall]] = {
    "default": ScoutAnalogiesCall,
}

SCOUT_PARADIGM_CASES_CALL_CLASSES: dict[str, type[ScoutParadigmCasesCall]] = {
    "default": ScoutParadigmCasesCall,
}

SCOUT_FACTCHECKS_CALL_CLASSES: dict[str, type[ScoutFactchecksCall]] = {
    "default": ScoutFactchecksCall,
}

SCOUT_WEB_QUESTIONS_CALL_CLASSES: dict[str, type[ScoutWebQuestionsCall]] = {
    "default": ScoutWebQuestionsCall,
}

SCOUT_DEEP_QUESTIONS_CALL_CLASSES: dict[str, type[ScoutDeepQuestionsCall]] = {
    "default": ScoutDeepQuestionsCall,
}

SCOUT_C_HOW_TRUE_CALL_CLASSES: dict[str, type[ScoutCHowTrueCall]] = {
    "default": ScoutCHowTrueCall,
}

SCOUT_C_HOW_FALSE_CALL_CLASSES: dict[str, type[ScoutCHowFalseCall]] = {
    "default": ScoutCHowFalseCall,
}

SCOUT_C_CRUXES_CALL_CLASSES: dict[str, type[ScoutCCruxesCall]] = {
    "default": ScoutCCruxesCall,
}

SCOUT_C_RELEVANT_EVIDENCE_CALL_CLASSES: dict[str, type[ScoutCRelevantEvidenceCall]] = {
    "default": ScoutCRelevantEvidenceCall,
}

SCOUT_C_STRESS_TEST_CASES_CALL_CLASSES: dict[str, type[ScoutCStressTestCasesCall]] = {
    "default": ScoutCStressTestCasesCall,
}

SCOUT_C_ROBUSTIFY_CALL_CLASSES: dict[str, type[ScoutCRobustifyCall]] = {
    "default": ScoutCRobustifyCall,
}

SCOUT_C_STRENGTHEN_CALL_CLASSES: dict[str, type[ScoutCStrengthenCall]] = {
    "default": ScoutCStrengthenCall,
}
