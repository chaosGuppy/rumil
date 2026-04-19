"""Registries mapping CallTypes / variant names to CallRunner classes.

Two registries live here:

- ``ASSESS_CALL_CLASSES`` — variants of the assess call
  (``"default"`` vs ``"big"``), keyed by ``settings.assess_call_variant``.
  Assess is the only call type with multiple variants.
- ``CALL_RUNNER_CLASSES`` — the single source of truth mapping every
  dispatchable ``CallType`` to its concrete ``CallRunner`` class. Callers
  (chat dispatch, CLI single-call runner, etc.) should read through
  ``get_call_runner_class()`` rather than importing the concrete classes
  directly.

For ``CallType.ASSESS``, the registry binds to ``AssessCall`` (the default
variant). Variant selection (``ASSESS_CALL_CLASSES[settings.assess_call_variant]``)
is handled separately at call sites that care — see
``rumil.orchestrators.common.assess_question``.
"""

from rumil.calls.assess import AssessCall, BigAssessCall
from rumil.calls.find_considerations import FindConsiderationsCall
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
from rumil.calls.web_research import WebResearchCall
from rumil.models import CallType

ASSESS_CALL_CLASSES: dict[str, type[AssessCall]] = {
    "default": AssessCall,
    "big": BigAssessCall,
}


CALL_RUNNER_CLASSES: dict[CallType, type[CallRunner]] = {
    CallType.FIND_CONSIDERATIONS: FindConsiderationsCall,
    CallType.ASSESS: AssessCall,
    CallType.WEB_RESEARCH: WebResearchCall,
    CallType.SCOUT_SUBQUESTIONS: ScoutSubquestionsCall,
    CallType.SCOUT_ESTIMATES: ScoutEstimatesCall,
    CallType.SCOUT_HYPOTHESES: ScoutHypothesesCall,
    CallType.SCOUT_ANALOGIES: ScoutAnalogiesCall,
    CallType.SCOUT_PARADIGM_CASES: ScoutParadigmCasesCall,
    CallType.SCOUT_FACTCHECKS: ScoutFactchecksCall,
    CallType.SCOUT_WEB_QUESTIONS: ScoutWebQuestionsCall,
    CallType.SCOUT_DEEP_QUESTIONS: ScoutDeepQuestionsCall,
    CallType.SCOUT_C_HOW_TRUE: ScoutCHowTrueCall,
    CallType.SCOUT_C_HOW_FALSE: ScoutCHowFalseCall,
    CallType.SCOUT_C_CRUXES: ScoutCCruxesCall,
    CallType.SCOUT_C_RELEVANT_EVIDENCE: ScoutCRelevantEvidenceCall,
    CallType.SCOUT_C_STRESS_TEST_CASES: ScoutCStressTestCasesCall,
    CallType.SCOUT_C_ROBUSTIFY: ScoutCRobustifyCall,
    CallType.SCOUT_C_STRENGTHEN: ScoutCStrengthenCall,
}


def get_call_runner_class(call_type: CallType) -> type[CallRunner]:
    """Look up the CallRunner class for a dispatchable CallType.

    Raises ValueError if the CallType has no registered runner (e.g. not
    dispatchable from prioritization, or a call type managed via bespoke
    machinery like orchestrator dispatch handlers).
    """
    cls = CALL_RUNNER_CLASSES.get(call_type)
    if cls is None:
        raise ValueError(
            f"No CallRunner registered for {call_type.value}. "
            f"Registered: {sorted(c.value for c in CALL_RUNNER_CLASSES)}"
        )
    return cls
