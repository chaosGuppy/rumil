"""Registries mapping variant names to call classes.

Each call type (find_considerations, assess, ingest) has its own registry so
callers can look up the concrete class by a short string name stored in settings.
"""

from rumil.calls.assess import AssessCall, EmbeddingAssessCall
from rumil.calls.assess_concept import AssessConceptCall
from rumil.calls.ingest import EmbeddingIngestCall, IngestCall
from rumil.calls.find_considerations import EmbeddingFindConsiderationsCall, FindConsiderationsCall
from rumil.calls.scout_analogies import ScoutAnalogiesCall
from rumil.calls.scout_facts_to_check import ScoutFactsToCheckCall
from rumil.calls.scout_paradigm_cases import ScoutParadigmCasesCall
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

SCOUT_FACTS_TO_CHECK_CALL_CLASSES: dict[str, type[ScoutFactsToCheckCall]] = {
    "default": ScoutFactsToCheckCall,
}
