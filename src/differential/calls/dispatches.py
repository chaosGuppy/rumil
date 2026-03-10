"""Dispatch definitions: tool schemas and registry for prioritization dispatches."""

from dataclasses import dataclass
from typing import Generic, TypeVar

from differential.llm import Tool
from differential.models import (
    AssessDispatchPayload,
    BaseDispatchPayload,
    CallType,
    Dispatch,
    PrioritizationDispatchPayload,
    ScoutDispatchPayload,
)
from differential.moves.base import MoveState

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
    ) -> Tool:
        """Return a Tool bound to a call's mutable state."""

        def fn(inp: dict) -> str:
            validated = self.schema(**inp)

            if subtree_ids is not None:
                raw_qid = inp.get("question_id", "")
                resolved = raw_qid
                if short_id_map and raw_qid in short_id_map:
                    resolved = short_id_map[raw_qid]
                elif len(raw_qid) <= 8:
                    for full_id in state.created_page_ids:
                        if full_id.startswith(raw_qid):
                            resolved = full_id
                            break
                if (
                    resolved not in subtree_ids
                    and resolved not in state.created_page_ids
                ):
                    return (
                        f"Question {raw_qid} is outside the scope subtree and was not "
                        "created during this call. You can only dispatch on the scope "
                        "question, its descendant sub-questions, or questions you just created."
                    )

            state.dispatches.append(
                Dispatch(call_type=self.call_type, payload=validated)
            )
            return "Dispatch recorded."

        return Tool(
            name=self.name,
            description=self.description,
            input_schema=self.schema.model_json_schema(),
            fn=fn,
        )


DISPATCH_DEFS: dict[CallType, DispatchDef] = {
    CallType.SCOUT: DispatchDef(
        call_type=CallType.SCOUT,
        name="dispatch_scout",
        description=(
            "Dispatch scout rounds for a question. Finds missing considerations. "
            "Each round consumes 1 unit of budget. Runs up to max_rounds rounds, "
            "stopping early when remaining fruit falls below fruit_threshold. "
            "Budget cost: between 1 and max_rounds (inclusive)."
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
}
