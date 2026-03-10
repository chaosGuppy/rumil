"""Test for the orchestrator's investigate_question."""

import pytest

from differential.orchestrator import Orchestrator


@pytest.mark.llm
def test_investigate_question_creates_pages(tmp_db, question_page):
    """Budget-1 investigation should produce at least one new page."""
    tmp_db.init_budget(1)
    orch = Orchestrator(tmp_db)
    orch.investigate_question(question_page.id, budget=1)

    rows = (
        tmp_db.client.table("pages").select("id").eq("run_id", tmp_db.run_id).execute()
    )
    page_ids = [r["id"] for r in rows.data]
    new_ids = [pid for pid in page_ids if pid != question_page.id]
    assert len(new_ids) >= 1, f"Expected new pages, only found the original question"
