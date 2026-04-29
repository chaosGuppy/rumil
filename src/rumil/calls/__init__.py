"""Call types for the research workspace."""

from rumil.calls.assess import AssessCall, BigAssessCall
from rumil.calls.call_registry import ASSESS_CALL_CLASSES
from rumil.calls.create_view import CreateViewCall
from rumil.calls.create_view_max_effort import CreateViewMaxEffortCall
from rumil.calls.critique_artefact import CritiqueArtefactCall
from rumil.calls.critique_artefact_request_only import RequestOnlyCritiqueArtefactCall
from rumil.calls.find_considerations import FindConsiderationsCall
from rumil.calls.freeform_view import CreateFreeformViewCall, UpdateFreeformViewCall
from rumil.calls.generate_artefact import GenerateArtefactCall
from rumil.calls.generate_spec import GenerateSpecCall
from rumil.calls.impact_filtered_context import ImpactFilteredContext
from rumil.calls.ingest import IngestCall
from rumil.calls.link_subquestions import LinkSubquestionsCall
from rumil.calls.red_team import RedTeamCall
from rumil.calls.refine_spec import RefineSpecCall
from rumil.calls.scout_analogies import ScoutAnalogiesCall
from rumil.calls.scout_c_cruxes import ScoutCCruxesCall
from rumil.calls.scout_c_how_false import ScoutCHowFalseCall
from rumil.calls.scout_c_how_true import ScoutCHowTrueCall
from rumil.calls.scout_c_relevant_evidence import ScoutCRelevantEvidenceCall
from rumil.calls.scout_c_robustify import ScoutCRobustifyCall
from rumil.calls.scout_c_strengthen import ScoutCStrengthenCall
from rumil.calls.scout_c_stress_test_cases import ScoutCStressTestCasesCall
from rumil.calls.scout_deep_questions import ScoutDeepQuestionsCall
from rumil.calls.scout_estimates import ScoutEstimatesCall
from rumil.calls.scout_factchecks import ScoutFactchecksCall
from rumil.calls.scout_hypotheses import ScoutHypothesesCall
from rumil.calls.scout_paradigm_cases import ScoutParadigmCasesCall
from rumil.calls.scout_subquestions import ScoutSubquestionsCall
from rumil.calls.scout_web_questions import ScoutWebQuestionsCall
from rumil.calls.stages import CallRunner
from rumil.calls.update_view import UpdateViewCall
from rumil.calls.update_view_max_effort import UpdateViewMaxEffortCall
from rumil.calls.web_research import WebResearchCall

__all__ = [
    "ASSESS_CALL_CLASSES",
    "AssessCall",
    "BigAssessCall",
    "CallRunner",
    "CreateFreeformViewCall",
    "CreateViewCall",
    "CreateViewMaxEffortCall",
    "CritiqueArtefactCall",
    "FindConsiderationsCall",
    "GenerateArtefactCall",
    "GenerateSpecCall",
    "ImpactFilteredContext",
    "IngestCall",
    "LinkSubquestionsCall",
    "RedTeamCall",
    "RefineSpecCall",
    "RequestOnlyCritiqueArtefactCall",
    "ScoutAnalogiesCall",
    "ScoutCCruxesCall",
    "ScoutCHowFalseCall",
    "ScoutCHowTrueCall",
    "ScoutCRelevantEvidenceCall",
    "ScoutCRobustifyCall",
    "ScoutCStrengthenCall",
    "ScoutCStressTestCasesCall",
    "ScoutDeepQuestionsCall",
    "ScoutEstimatesCall",
    "ScoutFactchecksCall",
    "ScoutHypothesesCall",
    "ScoutParadigmCasesCall",
    "ScoutSubquestionsCall",
    "ScoutWebQuestionsCall",
    "UpdateFreeformViewCall",
    "UpdateViewCall",
    "UpdateViewMaxEffortCall",
    "WebResearchCall",
]
