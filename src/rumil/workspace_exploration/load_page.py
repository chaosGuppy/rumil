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
from rumil.tracing.tracer import CallTrace

_DETAIL_MAP: dict[str, PageDetail] = {
    "abstract": PageDetail.ABSTRACT,
    "content": PageDetail.CONTENT,
}


class _LoadPageInput(BaseModel):
    page_id: str = Field(
        description="Short ID (first 8 chars) or full UUID of a page",
    )
    detail: str = Field(
        default="abstract",
        description=(
            "Level of detail: 'abstract' (short summary, default) "
            "or 'content' (full text)"
        ),
    )


def make_load_page_tool(db: DB, trace: CallTrace) -> Tool:
    """Build a page-loading tool that returns abstract or full content."""

    async def fn(args: dict) -> str:
        payload = _LoadPageInput.model_validate(args)
        full_id = await db.resolve_page_id(payload.page_id.strip())
        if not full_id:
            return f"Page '{payload.page_id}' not found."
        page = await db.get_page(full_id)
        if not page:
            return f"Page '{payload.page_id}' not found."
        detail = _DETAIL_MAP.get(payload.detail, PageDetail.ABSTRACT)
        return await format_page(page, detail, db=db)

    return Tool(
        name="load_page",
        description=(
            "Load a page's abstract (default) or full content. Use 'abstract' "
            "for a concise summary, 'content' for the full text."
        ),
        input_schema=_LoadPageInput.model_json_schema(),
        fn=fn,
    )
