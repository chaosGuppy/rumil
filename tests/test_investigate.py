"""Test for the orchestrator's run loop."""

import pytest

from rumil.models import Page, PageLayer, PageType, Workspace
from rumil.orchestrators import Orchestrator


@pytest.mark.integration
async def test_investigate_creates_pages(tmp_db):
    """Investigation should produce at least one new page."""
    question = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=(
            "What are the main factors that determine whether a city's "
            "investment in protected bike lanes leads to a sustained increase "
            "in cycling commuting rates?"
        ),
        headline=("What determines whether protected bike lanes increase cycling commute rates?"),
    )
    await tmp_db.save_page(question)

    await tmp_db.init_budget(8)
    orch = Orchestrator(tmp_db)
    await orch.run(question.id)

    rows = await tmp_db.client.table("pages").select("id").eq("run_id", tmp_db.run_id).execute()
    page_ids = [r["id"] for r in rows.data]
    new_ids = [pid for pid in page_ids if pid != question.id]
    assert len(new_ids) >= 1, "Expected new pages, only found the original question"
