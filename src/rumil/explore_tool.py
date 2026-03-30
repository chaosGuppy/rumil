"""Factory for the explore_page MCP tool."""

from claude_agent_sdk import tool
from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.evaluate.explore import explore_page_impl


class _ExplorePageInput(BaseModel):
    page_id: str = Field(description="Page ID (short 8-char prefix or full UUID)")


def make_explore_tool(db: DB):
    """Create the explore_page MCP tool definition, closing over *db*."""

    @tool(
        "explore_page",
        "Explore the local graph around a page. Returns the page and its "
        "neighbors at varying detail levels based on graph distance.",
        _ExplorePageInput.model_json_schema(),
    )
    async def explore_page(args: dict) -> dict:
        page_id = args["page_id"]
        result = await explore_page_impl(page_id, db)
        return {"content": [{"type": "text", "text": result}]}

    return explore_page
