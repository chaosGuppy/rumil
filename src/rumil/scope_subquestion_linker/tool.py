"""Custom Tools for the linker agent: subgraph rendering and result submission."""

import logging
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ValidationError

from rumil.database import DB
from rumil.llm import Tool
from rumil.settings import get_settings
from rumil.tracing.tracer import CallTrace
from rumil.workspace_exploration import make_explore_subgraph_tool

log = logging.getLogger(__name__)


SUBMIT_TOOL_NAME = "submit_linked_subquestions"


class LinkerResult(BaseModel):
    question_ids: list[str] = Field(
        description=(
            "Short IDs (8-char prefix) or full UUIDs of existing questions that pass "
            "the relevance bar and should be linked as subquestions of the scope. "
            "Empty list if no candidates qualify."
        )
    )


@dataclass
class SubmitHolder:
    result: LinkerResult | None = None
    raw_inputs: list[dict] = field(default_factory=list)


def make_render_subgraph_tool(db: DB, trace: CallTrace) -> Tool:
    """Build the subgraph exploration tool for the linker agent."""
    return make_explore_subgraph_tool(
        db,
        trace,
        max_pages=get_settings().scope_subquestion_linker_subgraph_max_pages,
        questions_only=True,
    )


def make_submit_tool(holder: SubmitHolder) -> Tool:
    """Build the final-answer submission tool.

    The agent calls this exactly once as its final action. The validated payload
    is captured on *holder*, and the runner reads it after the loop.
    """

    async def fn(args: dict) -> str:
        holder.raw_inputs.append(args)
        try:
            result = LinkerResult.model_validate(args)
        except ValidationError as exc:
            log.warning("submit_linked_subquestions: schema validation failed: %s", exc)
            return (
                "Error: payload did not match the required schema. "
                f"Details: {exc.errors()}. Please call this tool again with a valid "
                "payload, or with an empty `question_ids` list if you have no candidates."
            )
        holder.result = result
        return (
            f"Submission accepted: {len(result.question_ids)} question(s) recorded. "
            "End your turn now."
        )

    return Tool(
        name=SUBMIT_TOOL_NAME,
        description=(
            "Submit your final list of proposed subquestion links and end your "
            "investigation. Call this tool exactly ONCE, as your very last action, "
            "after you have finished exploring. Do not call any other tool after "
            "this. If you have no candidates that pass the relevance bar, call this "
            "with an empty `question_ids` list."
        ),
        input_schema=LinkerResult.model_json_schema(),
        fn=fn,
    )
