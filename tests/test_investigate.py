"""Test for the orchestrator's run loop."""

import pytest

from rumil.orchestrator import Orchestrator


@pytest.mark.llm
async def test_investigate_creates_pages(tmp_db, question_page):
    """Budget-1 investigation should produce at least one new page."""
    await tmp_db.init_budget(1)
    orch = Orchestrator(tmp_db)
    await orch.run(question_page.id)

    rows = (
        await tmp_db.client.table("pages").select("id").eq("run_id", tmp_db.run_id).execute()
    )
    page_ids = [r["id"] for r in rows.data]
    new_ids = [pid for pid in page_ids if pid != question_page.id]
    assert len(new_ids) >= 1, f"Expected new pages, only found the original question"
