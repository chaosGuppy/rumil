"""Call types for the research workspace."""

from rumil.calls.assess import AssessCall, BigAssessCall
from rumil.calls.call_registry import ASSESS_CALL_CLASSES
from rumil.calls.ingest import IngestCall
from rumil.calls.prioritization import run_prioritization
from rumil.calls.find_considerations import FindConsiderationsCall
from rumil.calls.stages import CallRunner
from rumil.calls.web_research import WebResearchCall
from rumil.calls.scout_analogies import ScoutAnalogiesCall
from rumil.calls.scout_paradigm_cases import ScoutParadigmCasesCall
from rumil.calls.scout_estimates import ScoutEstimatesCall
from rumil.calls.scout_hypotheses import ScoutHypothesesCall
from rumil.calls.scout_factchecks import ScoutFactchecksCall
from rumil.calls.scout_subquestions import ScoutSubquestionsCall
from rumil.calls.scout_c_cruxes import ScoutCCruxesCall
from rumil.calls.scout_c_how_false import ScoutCHowFalseCall
from rumil.calls.scout_c_how_true import ScoutCHowTrueCall
from rumil.calls.scout_c_relevant_evidence import ScoutCRelevantEvidenceCall
from rumil.calls.scout_c_robustify import ScoutCRobustifyCall
from rumil.calls.scout_c_strengthen import ScoutCStrengthenCall
from rumil.calls.scout_c_stress_test_cases import ScoutCStressTestCasesCall
from rumil.calls.scout_deep_questions import ScoutDeepQuestionsCall
from rumil.calls.scout_web_questions import ScoutWebQuestionsCall
from rumil.calls.link_subquestions import LinkSubquestionsCall
from rumil.calls.create_view import CreateViewCall

__all__ = [
    "CallRunner",
    "AssessCall",
    "BigAssessCall",
    "ASSESS_CALL_CLASSES",
    "IngestCall",
    "FindConsiderationsCall",
    "WebResearchCall",
    "ScoutSubquestionsCall",
    "ScoutEstimatesCall",
    "ScoutHypothesesCall",
    "ScoutAnalogiesCall",
    "ScoutParadigmCasesCall",
    "ScoutFactchecksCall",
    "ScoutWebQuestionsCall",
    "ScoutDeepQuestionsCall",
    "ScoutCHowTrueCall",
    "ScoutCHowFalseCall",
    "ScoutCCruxesCall",
    "ScoutCRelevantEvidenceCall",
    "ScoutCRobustifyCall",
    "ScoutCStrengthenCall",
    "ScoutCStressTestCasesCall",
    "LinkSubquestionsCall",
    "CreateViewCall",
    "run_prioritization",
]
