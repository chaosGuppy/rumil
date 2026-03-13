"""Tests for temporal filtering of workspace maps and last-call-time lookup."""

import asyncio
from datetime import datetime, timezone

from rumil.models import (
    Call,
    CallStatus,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.workspace_map import build_workspace_map


async def _make_question(db, text):
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=text,
        summary=text[:120],
    )
    await db.save_page(page)
    return page


async def _make_claim(db, text):
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=text,
        summary=text[:120],
        epistemic_status=4.0,
        epistemic_type='well-established',
    )
    await db.save_page(page)
    return page


async def _link_consideration(db, claim, question):
    await db.save_link(
        PageLink(
            from_page_id=claim.id,
            to_page_id=question.id,
            link_type=LinkType.CONSIDERATION,
            strength=4.0,
            reasoning='test',
        )
    )


async def test_last_successful_call_time_returns_none_when_no_calls(tmp_db):
    """No previous calls means None — run_call should use the full map."""
    question = await _make_question(tmp_db, 'Test question')
    result = await tmp_db.get_last_successful_call_time(
        CallType.SCOUT, question.id,
    )
    assert result is None


async def test_last_successful_call_time_returns_completed_at(tmp_db):
    """After a completed call, the timestamp is returned."""
    question = await _make_question(tmp_db, 'Test question')
    call = Call(
        call_type=CallType.SCOUT,
        workspace=Workspace.RESEARCH,
        scope_page_id=question.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)

    call.status = CallStatus.COMPLETE
    call.completed_at = datetime.now(timezone.utc)
    await tmp_db.save_call(call)

    result = await tmp_db.get_last_successful_call_time(
        CallType.SCOUT, question.id,
    )
    assert result is not None
    assert abs((result - call.completed_at).total_seconds()) < 2


async def test_last_successful_call_time_ignores_other_call_types(tmp_db):
    """A completed assess call should not affect scout lookup."""
    question = await _make_question(tmp_db, 'Test question')
    call = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question.id,
        status=CallStatus.COMPLETE,
        completed_at=datetime.now(timezone.utc),
    )
    await tmp_db.save_call(call)

    result = await tmp_db.get_last_successful_call_time(
        CallType.SCOUT, question.id,
    )
    assert result is None


async def test_filtered_map_excludes_old_claims(tmp_db):
    """Claims created before the cutoff should not appear in the filtered map."""
    question = await _make_question(tmp_db, 'Test question')
    old_claim = await _make_claim(tmp_db, 'Old claim from before')
    await _link_consideration(tmp_db, old_claim, question)

    cutoff = datetime.now(timezone.utc)
    await asyncio.sleep(0.05)

    new_claim = await _make_claim(tmp_db, 'New claim from after')
    await _link_consideration(tmp_db, new_claim, question)

    filtered_map, filtered_ids = await build_workspace_map(
        tmp_db, created_after=cutoff,
    )

    assert new_claim.id[:8] in filtered_ids
    assert old_claim.id[:8] not in filtered_ids
    assert 'New claim' in filtered_map
    assert 'Old claim' not in filtered_map


async def test_filtered_map_omits_questions_with_no_new_content(tmp_db):
    """Questions that have only old content should be excluded entirely."""
    q_old = await _make_question(tmp_db, 'Old question')
    old_claim = await _make_claim(tmp_db, 'Stale claim')
    await _link_consideration(tmp_db, old_claim, q_old)

    cutoff = datetime.now(timezone.utc)
    await asyncio.sleep(0.05)

    q_new = await _make_question(tmp_db, 'Fresh question')
    new_claim = await _make_claim(tmp_db, 'Fresh claim')
    await _link_consideration(tmp_db, new_claim, q_new)

    filtered_map, filtered_ids = await build_workspace_map(
        tmp_db, created_after=cutoff,
    )

    assert q_new.id[:8] in filtered_ids
    assert q_old.id[:8] not in filtered_ids
    assert 'Fresh question' in filtered_map
    assert 'Old question' not in filtered_map


async def test_full_map_includes_everything(tmp_db):
    """Without created_after, all pages appear."""
    question = await _make_question(tmp_db, 'Test question')
    claim = await _make_claim(tmp_db, 'A claim')
    await _link_consideration(tmp_db, claim, question)

    full_map, full_ids = await build_workspace_map(tmp_db)

    assert question.id[:8] in full_ids
    assert claim.id[:8] in full_ids
    assert 'Test question' in full_map
    assert 'A claim' in full_map
