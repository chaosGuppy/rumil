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

    Accepts a list of page IDs so the caller can fetch many pages in a
    single tool call rather than one at a time. *default_detail* sets the
    detail level used when the LLM omits the ``detail`` parameter (default
    ``"content"``).
    """

    class _LoadPageInput(BaseModel):
        page_ids: list[str] = Field(
            description=(
                "One or more page IDs to load (short 8-char prefixes or full "
                "UUIDs). Prefer batching several IDs in one call over issuing "
                "many sequential calls."
            ),
            min_length=1,
        )
        detail: str = Field(
            default=default_detail,
            description=("Level of detail: 'content' (full text) or 'abstract' (short summary)"),
        )

    async def fn(args: dict) -> str:
        # Backward-compat: accept legacy `page_id` (singular) as a one-element list.
        if "page_ids" not in args and "page_id" in args:
            args = {**args, "page_ids": [args["page_id"]]}
        payload = _LoadPageInput.model_validate(args)
        detail = _DETAIL_MAP.get(payload.detail, PageDetail.CONTENT)

        rendered: list[str] = []
        for raw_id in payload.page_ids:
            page_id = raw_id.strip()
            full_id = await db.resolve_page_id(page_id)
            if not full_id:
                rendered.append(f"## `{page_id}`\n\nPage not found.")
                continue
            page = await db.get_page(full_id)
            if not page:
                rendered.append(f"## `{page_id}`\n\nPage not found.")
                continue
            body = await format_page(
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
                    response=body,
                )
            )
            rendered.append(body)

        return "\n\n---\n\n".join(rendered)

    return Tool(
        name="load_page",
        description=(
            "Load one or more pages by short ID. Returns full content by "
            "default; pass detail='abstract' for concise summaries. Pass "
            "multiple IDs in `page_ids` to batch rather than calling this "
            "tool repeatedly."
        ),
        input_schema=_LoadPageInput.model_json_schema(),
        fn=fn,
    )
