"""Raw page-loading tool for LLM agents.

This is the non-move version of page loading — a lightweight Tool closure
that resolves a page ID and returns formatted content. It does not record
moves or interact with MoveState; use the move in ``rumil.moves.load_page``
when you need move-level tracking.
"""

from pydantic import BaseModel, Field

from rumil.context import format_page
from rumil.database import DB
from rumil.llm import Tool
from rumil.models import PageDetail
from rumil.tracing.trace_events import LoadPageEvent
from rumil.tracing.tracer import CallTrace

_DETAIL_MAP: dict[str, PageDetail] = {
    "abstract": PageDetail.ABSTRACT,
    "content": PageDetail.CONTENT,
}


def make_load_page_tool(
    db: DB,
    trace: CallTrace,
    *,
    default_detail: str = "content",
    highlight_run_id: str | None = None,
) -> Tool:
    """Build a page-loading tool that returns abstract or full content.

    *default_detail* sets the detail level used when the LLM omits the
    ``detail`` parameter (default ``"content"``).
    """

    class _LoadPageInput(BaseModel):
        page_id: str = Field(
            description="Short ID (first 8 chars) or full UUID of a page",
        )
        detail: str = Field(
            default=default_detail,
            description=("Level of detail: 'content' (full text) or 'abstract' (short summary)"),
        )

    async def fn(args: dict) -> str:
        payload = _LoadPageInput.model_validate(args)
        full_id = await db.resolve_page_id(payload.page_id.strip())
        if not full_id:
            return f"Page '{payload.page_id}' not found."
        page = await db.get_page(full_id)
        if not page:
            return f"Page '{payload.page_id}' not found."
        detail = _DETAIL_MAP.get(payload.detail, PageDetail.CONTENT)
        result = await format_page(
            page,
            detail,
            db=db,
            highlight_run_id=highlight_run_id,
            track=True,
            track_tags={"source": "workspace_load_tool"},
        )
        await trace.record(
            LoadPageEvent(
                page_id=page.id,
                page_headline=page.headline,
                detail=payload.detail,
                response=result,
            )
        )
        return result

    return Tool(
        name="load_page",
        description=(
            "Load a page by short ID. Returns full content by default; "
            "pass detail='abstract' for a concise summary."
        ),
        input_schema=_LoadPageInput.model_json_schema(),
        fn=fn,
    )
