"""Tests for DB.create_call."""

from rumil.models import CallStatus, CallType, Workspace


async def test_create_call_persists_and_returns_call(tmp_db, question_page):
    call = await tmp_db.create_call(CallType.FIND_CONSIDERATIONS, scope_page_id=question_page.id)
    assert call.call_type == CallType.FIND_CONSIDERATIONS
    assert call.scope_page_id == question_page.id
    assert call.status == CallStatus.PENDING

    fetched = await tmp_db.get_call(call.id)
    assert fetched is not None
    assert fetched.scope_page_id == question_page.id


async def test_create_call_with_all_options(tmp_db, question_page):
    call = await tmp_db.create_call(
        CallType.PRIORITIZATION,
        scope_page_id=question_page.id,
        budget_allocated=10,
        workspace=Workspace.PRIORITIZATION,
        context_page_ids=["c1", "c2"],
    )
    fetched = await tmp_db.get_call(call.id)
    assert fetched is not None
    assert fetched.workspace == Workspace.PRIORITIZATION
    assert fetched.budget_allocated == 10
    assert fetched.context_page_ids == ["c1", "c2"]


async def test_create_call_defaults_context_page_ids(tmp_db):
    call = await tmp_db.create_call(CallType.ASSESS)
    assert call.context_page_ids == []
