"""Dispatch definitions: tool schemas and registry for prioritization dispatches."""

import copy
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from rumil.llm import Tool
from rumil.models import (
    FindConsiderationsMode,
    AssessDispatchPayload,
    BaseDispatchPayload,
    CallType,
    Dispatch,
    PrioritizationDispatchPayload,
    RecurseDispatchPayload,
    ScopeOnlyDispatchPayload,
    ScoutAnalogiesDispatchPayload,
    ScoutDispatchPayload,
    ScoutParadigmCasesDispatchPayload,
    ScoutEstimatesDispatchPayload,
    ScoutFactsToCheckDispatchPayload,
    ScoutHypothesesDispatchPayload,
    ScoutSubquestionsDispatchPayload,
    WebResearchDispatchPayload,
)
from rumil.moves.base import MoveState

log = logging.getLogger(__name__)

S = TypeVar("S", bound=BaseDispatchPayload)


@dataclass
class DispatchDef(Generic[S]):
    """Definition of a dispatch type: its identity, tool schema, and call type."""

    call_type: CallType
    name: str
    description: str
    schema: type[S]

    def bind(
        self,
        state: MoveState,
        subtree_ids: set[str] | None = None,
        short_id_map: dict[str, str] | None = None,
        scope_question_id: str | None = None,
        allowed_modes: Sequence[FindConsiderationsMode] | None = None,
    ) -> Tool:
        """Return a Tool bound to a call's mutable state."""

        async def fn(inp: dict) -> str:
            validated = self.schema(**inp)

            if allowed_modes and hasattr(validated, 'mode'):
                if validated.mode not in allowed_modes:
                    allowed_str = ', '.join(m.value for m in allowed_modes)
                    return (
                        f"Invalid mode '{validated.mode.value}'. "
                        f"Allowed modes: {allowed_str}"
                    )

            if isinstance(validated, ScopeOnlyDispatchPayload) and scope_question_id:
                validated.question_id = scope_question_id

            state.dispatches.append(
                Dispatch(call_type=self.call_type, payload=validated)
            )
            log.debug(
                "Dispatch recorded: type=%s, question=%s",
                self.call_type.value,
                getattr(validated, 'question_id', '?')[:8],
            )
            return "Dispatch recorded."

        schema = self.schema.model_json_schema()
        if allowed_modes is not None:
            schema = _filter_mode_schema(schema, allowed_modes)

        return Tool(
            name=self.name,
            description=self.description,
            input_schema=schema,
            fn=fn,
        )


def _filter_mode_schema(
    schema: dict,
    allowed_modes: Sequence[FindConsiderationsMode],
) -> dict:
    """Deep-copy schema and restrict the mode enum to only allowed values."""
    schema = copy.deepcopy(schema)
    allowed_values = [m.value for m in allowed_modes]

    mode_def_key = 'FindConsiderationsMode'
    defs = schema.get('$defs', {})
    if mode_def_key in defs:
        filtered_def = dict(defs[mode_def_key])
        filtered_def['enum'] = [v for v in filtered_def.get('enum', []) if v in allowed_values]
        if 'properties' in schema and 'mode' in schema['properties']:
            mode_prop = schema['properties']['mode']
            mode_prop.pop('$ref', None)
            mode_prop.update(filtered_def)
            if mode_prop.get('default') not in allowed_values:
                mode_prop['default'] = allowed_values[0]
            mode_prop['description'] = (
                'Scout mode. Available: '
                + ', '.join(f"'{v}'" for v in allowed_values)
                + '.'
            )
        del defs[mode_def_key]
        if not defs:
            del schema['$defs']

    return schema


DISPATCH_DEFS: dict[CallType, DispatchDef] = {
    CallType.FIND_CONSIDERATIONS: DispatchDef(
        call_type=CallType.FIND_CONSIDERATIONS,
        name="dispatch_find_considerations",
        description=(
            "Dispatch find-considerations rounds for a question. Finds missing "
            "considerations. Each round consumes 1 unit of budget. Runs up to "
            "max_rounds rounds, stopping early when remaining fruit falls below "
            "fruit_threshold. Budget cost: between 1 and max_rounds (inclusive)."
        ),
        schema=ScoutDispatchPayload,
    ),
    CallType.ASSESS: DispatchDef(
        call_type=CallType.ASSESS,
        name="dispatch_assess",
        description=(
            "Dispatch an assessment for a question. Renders a judgement. "
            "Budget cost: exactly 1."
        ),
        schema=AssessDispatchPayload,
    ),
    CallType.PRIORITIZATION: DispatchDef(
        call_type=CallType.PRIORITIZATION,
        name="dispatch_prioritization",
        description=(
            "Dispatch a sub-prioritization for a question. Delegates structured "
            "investigation. Budget cost: exactly the budget you assign."
        ),
        schema=PrioritizationDispatchPayload,
    ),
    CallType.SCOUT_SUBQUESTIONS: DispatchDef(
        call_type=CallType.SCOUT_SUBQUESTIONS,
        name="dispatch_scout_subquestions",
        description=(
            "Dispatch a specialized scout that identifies informative subquestions "
            "for the scope question. Always targets the scope question. "
            "Budget cost: exactly 1."
        ),
        schema=ScoutSubquestionsDispatchPayload,
    ),
    CallType.SCOUT_ESTIMATES: DispatchDef(
        call_type=CallType.SCOUT_ESTIMATES,
        name="dispatch_scout_estimates",
        description=(
            "Dispatch a specialized scout that generates quantitative estimates "
            "bearing on the scope question. Always targets the scope question. "
            "Budget cost: exactly 1."
        ),
        schema=ScoutEstimatesDispatchPayload,
    ),
    CallType.SCOUT_HYPOTHESES: DispatchDef(
        call_type=CallType.SCOUT_HYPOTHESES,
        name="dispatch_scout_hypotheses",
        description=(
            "Dispatch a specialized scout that proposes competing hypotheses "
            "for the scope question. Always targets the scope question. "
            "Budget cost: exactly 1."
        ),
        schema=ScoutHypothesesDispatchPayload,
    ),
    CallType.SCOUT_ANALOGIES: DispatchDef(
        call_type=CallType.SCOUT_ANALOGIES,
        name="dispatch_scout_analogies",
        description=(
            "Dispatch a specialized scout that finds illuminating analogies "
            "for the scope question. Always targets the scope question. "
            "Budget cost: exactly 1."
        ),
        schema=ScoutAnalogiesDispatchPayload,
    ),
    CallType.SCOUT_PARADIGM_CASES: DispatchDef(
        call_type=CallType.SCOUT_PARADIGM_CASES,
        name="dispatch_scout_paradigm_cases",
        description=(
            "Dispatch a specialized scout that identifies concrete paradigm "
            "cases illuminating the scope question. Always targets the scope question. "
            "Budget cost: exactly 1."
        ),
        schema=ScoutParadigmCasesDispatchPayload,
    ),
    CallType.SCOUT_FACTS_TO_CHECK: DispatchDef(
        call_type=CallType.SCOUT_FACTS_TO_CHECK,
        name="dispatch_scout_facts_to_check",
        description=(
            "Dispatch a specialized scout that surfaces uncertain factual "
            "claims whose truth value could materially affect the answer "
            "to the scope question. Always targets the scope question. "
            "Budget cost: exactly 1."
        ),
        schema=ScoutFactsToCheckDispatchPayload,
    ),
    CallType.WEB_RESEARCH: DispatchDef(
        call_type=CallType.WEB_RESEARCH,
        name="dispatch_web_research",
        description=(
            "Dispatch web research for a question. Searches the web and extracts "
            "relevant claims. Budget cost: exactly 1."
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
