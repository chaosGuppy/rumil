"""Call types for the research workspace."""
from differential.calls.scout import run_scout
from differential.calls.assess import run_assess
from differential.calls.prioritization import run_prioritization
from differential.calls.ingest import run_ingest

__all__ = ["run_scout", "run_assess", "run_prioritization", "run_ingest"]
