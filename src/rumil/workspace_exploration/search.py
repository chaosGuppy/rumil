"""Semantic search tool for LLM agents.

Embeds a natural-language query and returns the top-K most similar pages
from the workspace, rendered at abstract level with links at headline level.
"""

from pydantic import BaseModel, Field

from rumil.context import format_page
from rumil.database import DB
from rumil.embeddings import search_pages
from rumil.llm import Tool
from rumil.models import PageDetail
from rumil.tracing.tracer import CallTrace


class _SearchInput(BaseModel):
    query: str = Field(
        description="Natural-language search query",
    )


def make_search_tool(
    db: DB,
    trace: CallTrace,
    *,
    max_results: int = 10,
    match_threshold: float = 0.5,
) -> Tool:
    """Build a semantic search tool over the workspace.

    Returns up to *max_results* pages whose abstract embeddings are most
    similar to the query, rendered at abstract level with links at headline
    level.
    """

    async def fn(args: dict) -> str:
        payload = _SearchInput.model_validate(args)
        results = await search_pages(
            db,
            payload.query,
            match_threshold=match_threshold,
            match_count=max_results,
            field_name="abstract",
        )
        if not results:
            return "No matching pages found."

        sections: list[str] = []
        for page, score in results:
            formatted = await format_page(
                page,
                PageDetail.ABSTRACT,
                linked_detail=PageDetail.HEADLINE,
                db=db,
                track=True,
                track_tags={"source": "search_tool"},
            )
            sections.append(f"--- similarity: {score:.3f} ---\n{formatted}")
        return "\n\n".join(sections)

    return Tool(
        name="search_workspace",
        description=(
            "Search the workspace for pages semantically similar to a "
            "natural-language query. Returns the most relevant pages with "
            "their abstracts and linked page headlines."
        ),
        input_schema=_SearchInput.model_json_schema(),
        fn=fn,
    )
