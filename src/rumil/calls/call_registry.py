"""Registries mapping variant names to call classes.

Each call type (scout, assess, ingest) has its own registry so callers can
look up the concrete class by a short string name stored in settings.
"""

from rumil.calls.assess import AssessCall, EmbeddingAssessCall
from rumil.calls.ingest import EmbeddingIngestCall, IngestCall
from rumil.calls.scout import EmbeddingScoutCall, ScoutCall

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
