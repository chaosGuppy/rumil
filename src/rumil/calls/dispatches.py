"""Dispatch definitions: tool schemas and registry for prioritization dispatches."""

import logging
from dataclasses import dataclass

from rumil.llm import Tool
from rumil.models import (
    AssessDispatchPayload,
    BaseDispatchPayload,
    CallType,
    Dispatch,
    MultiRoundFields,
    PrioritizationFields,
    RecurseClaimDispatchPayload,
    RecurseDispatchPayload,
    ScopeOnlyDispatchPayload,
    ScoutAnalogiesDispatchPayload,
    ScoutCCruxesDispatchPayload,
    ScoutCHowFalseDispatchPayload,
    ScoutCHowTrueDispatchPayload,
    ScoutCRelevantEvidenceDispatchPayload,
    ScoutCRobustifyDispatchPayload,
    ScoutCStrengthenDispatchPayload,
    ScoutCStressTestCasesDispatchPayload,
    ScoutDeepQuestionsDispatchPayload,
    ScoutDispatchPayload,
    ScoutEstimatesDispatchPayload,
    ScoutFactchecksDispatchPayload,
    ScoutHypothesesDispatchPayload,
    ScoutParadigmCasesDispatchPayload,
    ScoutSubquestionsDispatchPayload,
    ScoutWebQuestionsDispatchPayload,
    WebResearchDispatchPayload,
)
from rumil.moves.base import MoveState

log = logging.getLogger(__name__)


def estimate_dispatch_cost(d: Dispatch) -> int:
    """Estimate worst-case budget cost of a single dispatch."""
    p = d.payload
    if isinstance(p, PrioritizationFields):
        return p.budget
    if isinstance(p, MultiRoundFields):
        return p.max_rounds
    return 1


@dataclass
class DispatchDef[S: BaseDispatchPayload]:
    """Definition of a dispatch type: its identity, tool schema, and call type."""

    call_type: CallType
    name: str
    description: str
    schema: type[S]

    def bind(
        self,
        state: MoveState,
        short_id_map: dict[str, str] | None = None,
        scope_question_id: str | None = None,
    ) -> Tool:
        """Return a Tool bound to a call's mutable state."""

        async def fn(inp: dict) -> str:
            validated = self.schema(**inp)

            if isinstance(validated, ScopeOnlyDispatchPayload) and scope_question_id:
                validated.question_id = scope_question_id

            qid = getattr(validated, "question_id", None)
            if qid and not isinstance(validated, ScopeOnlyDispatchPayload):
                resolved = await state.db.resolve_page_id(qid)
                if resolved is None:
                    suggestion = ""
                    if len(qid) > 8:
                        hint = await state.db.resolve_page_id(qid[:8])
                        if hint:
                            suggestion = f" The closest match by short-id prefix is `{hint}`."
                    return (
                        f"Dispatch rejected: page id `{qid}` does not exist."
                        f"{suggestion} Re-dispatch with a corrected id."
                    )
                if resolved != qid:
                    validated.question_id = resolved

            dispatch = Dispatch(call_type=self.call_type, payload=validated)
            error = state.record_dispatch(dispatch)
            if error:
                return error

            log.debug(
                "Dispatch recorded: type=%s, question=%s",
                self.call_type.value,
                getattr(validated, "question_id", "?")[:8],
            )
            return "Dispatch recorded."

        return Tool(
            name=self.name,
            description=self.description,
            input_schema=self.schema.model_json_schema(),
            fn=fn,
        )


DISPATCH_DEFS: dict[CallType, DispatchDef] = {
    CallType.FIND_CONSIDERATIONS: DispatchDef(
        call_type=CallType.FIND_CONSIDERATIONS,
        name="dispatch_find_considerations",
        description=(
            "Surface the handful of considerations that would most move the next "
            "answer on a question. A targeted sharpener, not exploration: it "
            "explicitly avoids opening new lines of investigation whose payoff is "
            "deferred. Use when a question is close to a defensible answer but has "
            "specific gaps, or when budget is tight and you need to reach a "
            "defensible answer fast. Runs up to max_rounds rounds, stopping early "
            "when remaining fruit falls below fruit_threshold. Budget cost: between "
            "1 and max_rounds (inclusive), plus 1 auto-assess if targeting a "
            "subquestion."
        ),
        schema=ScoutDispatchPayload,
    ),
    CallType.ASSESS: DispatchDef(
        call_type=CallType.ASSESS,
        name="dispatch_assess",
        description=(
            "Dispatch an assessment for a question. Renders a judgement. "
            "Budget cost: exactly 1 (plus 1 auto-assess if targeting a "
            "subquestion, so 2 total)."
        ),
        schema=AssessDispatchPayload,
    ),
    CallType.SCOUT_SUBQUESTIONS: DispatchDef(
        call_type=CallType.SCOUT_SUBQUESTIONS,
        name="dispatch_scout_subquestions",
        description=(
            "Dispatch a specialized scout that identifies informative subquestions "
            "for the scope question. Always targets the scope question. "
            "Runs up to max_rounds rounds, stopping early when remaining "
            "fruit falls below fruit_threshold. "
            "Budget cost: between 1 and max_rounds (inclusive), plus 1 auto-assess if targeting a subquestion."
        ),
        schema=ScoutSubquestionsDispatchPayload,
    ),
    CallType.SCOUT_ESTIMATES: DispatchDef(
        call_type=CallType.SCOUT_ESTIMATES,
        name="dispatch_scout_estimates",
        description=(
            "Dispatch a specialized scout that generates quantitative estimates "
            "bearing on the scope question. Always targets the scope question. "
            "Runs up to max_rounds rounds, stopping early when remaining "
            "fruit falls below fruit_threshold. "
            "Budget cost: between 1 and max_rounds (inclusive), plus 1 auto-assess if targeting a subquestion."
        ),
        schema=ScoutEstimatesDispatchPayload,
    ),
    CallType.SCOUT_HYPOTHESES: DispatchDef(
        call_type=CallType.SCOUT_HYPOTHESES,
        name="dispatch_scout_hypotheses",
        description=(
            "Dispatch a specialized scout that proposes competing hypotheses "
            "for the scope question. Always targets the scope question. "
            "Runs up to max_rounds rounds, stopping early when remaining "
            "fruit falls below fruit_threshold. "
            "Budget cost: between 1 and max_rounds (inclusive), plus 1 auto-assess if targeting a subquestion."
        ),
        schema=ScoutHypothesesDispatchPayload,
    ),
    CallType.SCOUT_ANALOGIES: DispatchDef(
        call_type=CallType.SCOUT_ANALOGIES,
        name="dispatch_scout_analogies",
        description=(
            "Dispatch a specialized scout that finds illuminating analogies "
            "for the scope question. Always targets the scope question. "
            "Runs up to max_rounds rounds, stopping early when remaining "
            "fruit falls below fruit_threshold. "
            "Budget cost: between 1 and max_rounds (inclusive), plus 1 auto-assess if targeting a subquestion."
        ),
        schema=ScoutAnalogiesDispatchPayload,
    ),
    CallType.SCOUT_PARADIGM_CASES: DispatchDef(
        call_type=CallType.SCOUT_PARADIGM_CASES,
        name="dispatch_scout_paradigm_cases",
        description=(
            "Dispatch a specialized scout that identifies concrete paradigm "
            "cases illuminating the scope question. Always targets the scope question. "
            "Runs up to max_rounds rounds, stopping early when remaining "
            "fruit falls below fruit_threshold. "
            "Budget cost: between 1 and max_rounds (inclusive), plus 1 auto-assess if targeting a subquestion."
        ),
        schema=ScoutParadigmCasesDispatchPayload,
    ),
    CallType.SCOUT_FACTCHECKS: DispatchDef(
        call_type=CallType.SCOUT_FACTCHECKS,
        name="dispatch_scout_factchecks",
        description=(
            "Dispatch a specialized scout that surfaces uncertain factual "
            "claims whose truth value could materially affect the answer "
            "to the scope question. Always targets the scope question. "
            "Runs up to max_rounds rounds, stopping early when remaining "
            "fruit falls below fruit_threshold. "
            "Budget cost: between 1 and max_rounds (inclusive), plus 1 auto-assess if targeting a subquestion."
        ),
        schema=ScoutFactchecksDispatchPayload,
    ),
    CallType.SCOUT_WEB_QUESTIONS: DispatchDef(
        call_type=CallType.SCOUT_WEB_QUESTIONS,
        name="dispatch_scout_web_questions",
        description=(
            "Dispatch a specialized scout that identifies concrete factual "
            "questions answerable via web research, where the LLM does not "
            "already know the answer. Always targets the scope question. "
            "Runs up to max_rounds rounds, stopping early when remaining "
            "fruit falls below fruit_threshold. "
            "Budget cost: between 1 and max_rounds (inclusive), plus 1 auto-assess if targeting a subquestion."
        ),
        schema=ScoutWebQuestionsDispatchPayload,
    ),
    CallType.SCOUT_DEEP_QUESTIONS: DispatchDef(
        call_type=CallType.SCOUT_DEEP_QUESTIONS,
        name="dispatch_scout_deep_questions",
        description=(
            "Dispatch a specialized scout that identifies important questions "
            "requiring judgement, interpretation, or involved reasoning — "
            "questions that cannot be resolved by simply looking something up. "
            "Always targets the scope question. "
            "Runs up to max_rounds rounds, stopping early when remaining "
            "fruit falls below fruit_threshold. "
            "Budget cost: between 1 and max_rounds (inclusive), plus 1 auto-assess if targeting a subquestion."
        ),
        schema=ScoutDeepQuestionsDispatchPayload,
    ),
    CallType.SCOUT_C_HOW_TRUE: DispatchDef(
        call_type=CallType.SCOUT_C_HOW_TRUE,
        name="dispatch_scout_c_how_true",
        description=(
            "Dispatch a scout that identifies plausible causal mechanisms "
            "that would make the scope claim true. Always targets the scope claim. "
            "Runs up to max_rounds rounds, stopping early when remaining "
            "fruit falls below fruit_threshold. "
            "Budget cost: between 1 and max_rounds (inclusive), plus 1 auto-assess if targeting a subquestion."
        ),
        schema=ScoutCHowTrueDispatchPayload,
    ),
    CallType.SCOUT_C_HOW_FALSE: DispatchDef(
        call_type=CallType.SCOUT_C_HOW_FALSE,
        name="dispatch_scout_c_how_false",
        description=(
            "Dispatch a scout that identifies plausible causal stories "
            "compatible with observed evidence but in which the scope claim "
            "is false. Always targets the scope claim. "
            "Runs up to max_rounds rounds, stopping early when remaining "
            "fruit falls below fruit_threshold. "
            "Budget cost: between 1 and max_rounds (inclusive), plus 1 auto-assess if targeting a subquestion."
        ),
        schema=ScoutCHowFalseDispatchPayload,
    ),
    CallType.SCOUT_C_CRUXES: DispatchDef(
        call_type=CallType.SCOUT_C_CRUXES,
        name="dispatch_scout_c_cruxes",
        description=(
            "Dispatch a scout that identifies cruxes — specific points where "
            "how-true and how-false stories diverge, such that resolving them "
            "would be most informative. Always targets the scope claim. "
            "Runs up to max_rounds rounds, stopping early when remaining "
            "fruit falls below fruit_threshold. "
            "Budget cost: between 1 and max_rounds (inclusive), plus 1 auto-assess if targeting a subquestion."
        ),
        schema=ScoutCCruxesDispatchPayload,
    ),
    CallType.SCOUT_C_RELEVANT_EVIDENCE: DispatchDef(
        call_type=CallType.SCOUT_C_RELEVANT_EVIDENCE,
        name="dispatch_scout_c_relevant_evidence",
        description=(
            "Dispatch a scout that identifies evidence worth gathering that "
            "bears on the most important cruxes of the scope claim. Always "
            "targets the scope claim. "
            "Runs up to max_rounds rounds, stopping early when remaining "
            "fruit falls below fruit_threshold. "
            "Budget cost: between 1 and max_rounds (inclusive), plus 1 auto-assess if targeting a subquestion."
        ),
        schema=ScoutCRelevantEvidenceDispatchPayload,
    ),
    CallType.SCOUT_C_STRESS_TEST_CASES: DispatchDef(
        call_type=CallType.SCOUT_C_STRESS_TEST_CASES,
        name="dispatch_scout_c_stress_test_cases",
        description=(
            "Dispatch a scout that identifies concrete scenarios serving as "
            "hard tests for the scope claim, especially boundary cases where "
            "competing stories predict different outcomes. Always targets the "
            "scope claim. "
            "Runs up to max_rounds rounds, stopping early when remaining "
            "fruit falls below fruit_threshold. "
            "Budget cost: between 1 and max_rounds (inclusive), plus 1 auto-assess if targeting a subquestion."
        ),
        schema=ScoutCStressTestCasesDispatchPayload,
    ),
    CallType.SCOUT_C_ROBUSTIFY: DispatchDef(
        call_type=CallType.SCOUT_C_ROBUSTIFY,
        name="dispatch_scout_c_robustify",
        description=(
            "Dispatch a scout that suggests more robust variations of the "
            "scope claim — lower bounds, conditional versions, narrower scope, "
            "or weaker quantifiers that are more defensible. Always targets the "
            "scope claim. "
            "Runs up to max_rounds rounds, stopping early when remaining "
            "fruit falls below fruit_threshold. "
            "Budget cost: between 1 and max_rounds (inclusive), plus 1 auto-assess if targeting a subquestion."
        ),
        schema=ScoutCRobustifyDispatchPayload,
    ),
    CallType.SCOUT_C_STRENGTHEN: DispatchDef(
        call_type=CallType.SCOUT_C_STRENGTHEN,
        name="dispatch_scout_c_strengthen",
        description=(
            "Dispatch a scout that tries to make a high-credence claim more "
            "precise, specific, or stronger while maintaining that credence. "
            "Always targets the scope claim. "
            "Runs up to max_rounds rounds, stopping early when remaining "
            "fruit falls below fruit_threshold. "
            "Budget cost: between 1 and max_rounds (inclusive), plus 1 auto-assess if targeting a subquestion."
        ),
        schema=ScoutCStrengthenDispatchPayload,
    ),
    CallType.WEB_RESEARCH: DispatchDef(
        call_type=CallType.WEB_RESEARCH,
        name="dispatch_web_factcheck",
        description=(
            "Verify a specific factual claim via web search. Use ONLY on "
            "questions that target a concrete, searchable fact — verifying "
            "an assertion, looking up a figure or date, or finding known "
            "examples of a well-defined category. Do NOT use on broad, "
            "interpretive, hypothesis, or judgement questions. "
            "Budget cost: exactly 1 (plus 1 auto-assess if targeting a "
            "subquestion, so 2 total)."
        ),
        schema=WebResearchDispatchPayload,
    ),
}

RECURSE_DISPATCH_DEF: DispatchDef[RecurseDispatchPayload] = DispatchDef(
    call_type=CallType.PRIORITIZATION,
    name="recurse_into_subquestion",
    description=(
        "Recursively investigate a subquestion with its own two-phase "
        "prioritization cycle. Budget cost: exactly the budget you assign."
    ),
    schema=RecurseDispatchPayload,
)

RECURSE_CLAIM_DISPATCH_DEF: DispatchDef[RecurseClaimDispatchPayload] = DispatchDef(
    call_type=CallType.PRIORITIZATION,
    name="recurse_into_claim_investigation",
    description=(
        "Recursively investigate a claim with its own two-phase claim "
        "investigation cycle (how-true/how-false stories, cruxes, evidence). "
        "Budget cost: exactly the budget you assign."
    ),
    schema=RecurseClaimDispatchPayload,
)
