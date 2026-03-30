"""Factory for the explore_page MCP tool."""

from claude_agent_sdk import tool

from rumil.database import DB
from rumil.evaluate.explore import explore_page_impl


def make_explore_tool(db: DB):
    """Create the explore_page MCP tool definition, closing over *db*."""

    @tool(
        "explore_page",
        "Explore the local graph around a page. Returns the page and its "
        "neighbors at varying detail levels based on graph distance. "
        "Input a page ID (short 8-char prefix or full UUID).",
        {"page_id": str},
    )
    async def explore_page(args: dict) -> dict:
        page_id = args["page_id"]
        result = await explore_page_impl(page_id, db)
        return {"content": [{"type": "text", "text": result}]}

    return explore_page
