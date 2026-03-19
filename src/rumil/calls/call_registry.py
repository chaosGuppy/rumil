"""Registries mapping variant names to call classes.

Each call type (scout, assess, ingest) has its own registry so callers can
look up the concrete class by a short string name stored in settings.
"""

from rumil.calls.assess import AssessCall, EmbeddingAssessCall
from rumil.calls.assess_concept import AssessConceptCall
from rumil.calls.ingest import EmbeddingIngestCall, IngestCall
from rumil.calls.scout import EmbeddingScoutCall, ScoutCall
from rumil.calls.scout_analogies import ScoutAnalogiesCall
from rumil.calls.scout_concepts import ScoutConceptsCall
from rumil.calls.scout_estimates import ScoutEstimatesCall
from rumil.calls.scout_hypotheses import ScoutHypothesesCall
from rumil.calls.scout_subquestions import ScoutSubquestionsCall

SCOUT_CALL_CLASSES: dict[str, type[ScoutCall]] = {
    "default": ScoutCall,
    "embedding": EmbeddingScoutCall,
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
