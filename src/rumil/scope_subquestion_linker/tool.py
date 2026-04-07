"""Custom Tool that exposes `render_question_subgraph` to the linker agent."""

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.llm import Tool
from rumil.scope_subquestion_linker.subgraph import render_question_subgraph
from rumil.tracing.trace_events import RenderQuestionSubgraphEvent
from rumil.tracing.tracer import CallTrace


class _RenderSubgraphInput(BaseModel):
    page_id: str = Field(
        description="Short ID (first 8 chars) or full UUID of a question page",
    )


def make_render_subgraph_tool(db: DB, trace: CallTrace) -> Tool:
    """Build the `render_question_subgraph` Tool, closing over *db* and *trace*."""

    async def fn(args: dict) -> str:
        payload = _RenderSubgraphInput.model_validate(args)
        text = await render_question_subgraph(payload.page_id, db)

        headline = ""
        recorded_id = payload.page_id
        resolved = await db.resolve_page_id(payload.page_id)
        if resolved:
            recorded_id = resolved
            page = await db.get_page(resolved)
            if page:
                headline = page.headline

        await trace.record(
            RenderQuestionSubgraphEvent(
                page_id=recorded_id,
                page_headline=headline,
                response=text,
            )
        )
        return text

    return Tool(
        name="render_question_subgraph",
        description=(
            "Render a 3-hop subgraph of the question graph rooted at the given question "
            "page. Returns headlines only for the question and its children, "
            "grandchildren, and great-grandchildren. Use this to drill into any question "
            "short ID you see in the seed subgraphs or in earlier tool results."
        ),
        input_schema=_RenderSubgraphInput.model_json_schema(),
        fn=fn,
    )
