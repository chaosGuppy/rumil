"""Tests for question_id validation inside DispatchDef.bind's tool fn.

Background: an LLM mistyping a UUID for ``recurse_into_subquestion`` used
to silently land in MoveState.dispatches and then fail at orchestrator
fan-out time (the prioritization caller saw "no resolution" and
``continue``-d), so the LLM never learned its plan was incomplete. The
fix is two-layered:

1. ``DB.resolve_page_id`` falls back to first-8-char prefix match on long
   inputs that miss exact match — so the common case (correct prefix,
   garbled middle) silently round-trips to the canonical UUID and the
   dispatch lands as intended.
2. ``DispatchDef.bind``'s tool fn validates the question_id resolves
   before recording, returning an error string with a closest-match
   suggestion when it doesn't. The LLM gets a chance to retry.
"""

import pytest

from rumil.calls.dispatches import (
    DISPATCH_DEFS,
    RECURSE_DISPATCH_DEF,
)
from rumil.models import (
    CallType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.moves.base import MoveState


async def _save_question(tmp_db, headline: str = "test question") -> Page:
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
    )
    await tmp_db.save_page(page)
    return page


async def test_bogus_question_id_is_rejected_with_error_string(tmp_db, prioritization_call):
    """A dispatch targeting a fully-bogus UUID returns an error string and
    is NOT recorded in MoveState.dispatches."""
    state = MoveState(prioritization_call, tmp_db)
    tool = DISPATCH_DEFS[CallType.WEB_RESEARCH].bind(state)

    bogus = "deadbeef-0000-0000-0000-000000000000"
    result = await tool.fn({"question_id": bogus})

    assert "Dispatch rejected" in result
    assert bogus in result
    assert "Re-dispatch" in result
    assert state.dispatches == []


async def test_rejection_includes_closest_match_when_prefix_collides(tmp_db, prioritization_call):
    """When the bogus UUID's first 8 chars happen to match a real page,
    the rejection message points at it as a closest-match hint."""
    page = await _save_question(tmp_db)
    state = MoveState(prioritization_call, tmp_db)
    tool = DISPATCH_DEFS[CallType.WEB_RESEARCH].bind(state)

    # Construct a long input that exact-misses but whose first 8 chars
    # match the real page. The new resolve_page_id will recover this on
    # its own — so to exercise the rejection path with a closest-match
    # hint we need an input that's ambiguous beyond pos 8 but whose
    # first 8 chars only match this one page. We get that by making
    # the input long enough to miss and *not* a real id, but the
    # leading 8 chars must be correct + unique.
    # Easiest: use a sentinel that the resolver would consider bogus
    # outright (URL-shaped) and observe no closest-match. So instead
    # use mocker to force the first resolve_page_id to None while the
    # second (prefix probe) returns the real id.
    # Simpler approach: we already have full coverage of the
    # closest-match-omitted path elsewhere; here we verify only that
    # when a hint exists, it's surfaced.
    short = page.id[:8]
    # Patch resolve so the validator path reaches the closest-match probe.
    real_resolve = tmp_db.resolve_page_id
    call_count = {"n": 0}

    async def fake_resolve(pid: str):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None  # primary lookup misses
        return await real_resolve(pid)  # the qid[:8] probe hits

    tmp_db.resolve_page_id = fake_resolve  # type: ignore[method-assign]
    try:
        long_bogus = short + "-XXXX-XXXX-XXXX-XXXXXXXXXXXX"
        result = await tool.fn({"question_id": long_bogus})
    finally:
        tmp_db.resolve_page_id = real_resolve  # type: ignore[method-assign]

    assert "Dispatch rejected" in result
    assert "closest match" in result.lower()
    assert page.id in result
    assert state.dispatches == []


async def test_rejection_omits_suggestion_when_prefix_also_misses(tmp_db, prioritization_call):
    """When neither exact nor first-8-char prefix resolves, the rejection
    string does NOT contain a closest-match clause."""
    state = MoveState(prioritization_call, tmp_db)
    tool = DISPATCH_DEFS[CallType.WEB_RESEARCH].bind(state)

    bogus = "deadbeef-0000-0000-0000-000000000000"
    result = await tool.fn({"question_id": bogus})

    assert "Dispatch rejected" in result
    assert "closest match" not in result.lower()
    assert state.dispatches == []


async def test_short_input_resolves_and_dispatch_records_full_uuid(tmp_db, prioritization_call):
    """Passing the 8-char short ID resolves to the canonical full UUID, the
    dispatch is recorded with that full UUID."""
    page = await _save_question(tmp_db)
    state = MoveState(prioritization_call, tmp_db)
    tool = DISPATCH_DEFS[CallType.WEB_RESEARCH].bind(state)

    result = await tool.fn({"question_id": page.id[:8]})

    assert result == "Dispatch recorded."
    assert len(state.dispatches) == 1
    assert state.dispatches[0].payload.question_id == page.id


async def test_mistyped_long_uuid_is_silently_recovered(tmp_db, prioritization_call):
    """A long UUID with correct first-8 chars but garbled middle gets
    silently rewritten to the canonical UUID via the resolve fallback —
    no rejection, dispatch recorded against the right page. This is the
    headline scenario from the bug report."""
    page = await _save_question(tmp_db)
    state = MoveState(prioritization_call, tmp_db)
    tool = DISPATCH_DEFS[CallType.WEB_RESEARCH].bind(state)

    mistyped = page.id[:8] + "-XXXX-XXXX-XXXX-XXXXXXXXXXXX"
    assert mistyped != page.id

    result = await tool.fn({"question_id": mistyped})

    assert result == "Dispatch recorded."
    assert len(state.dispatches) == 1
    assert state.dispatches[0].payload.question_id == page.id


async def test_recurse_dispatch_same_validation(tmp_db, prioritization_call):
    """Recurse dispatches go through the same validator; the bug-report
    scenario was specifically about recurse_into_subquestion."""
    page = await _save_question(tmp_db)
    state = MoveState(prioritization_call, tmp_db)
    tool = RECURSE_DISPATCH_DEF.bind(state)

    bogus = "deadbeef-0000-0000-0000-000000000000"
    rejected = await tool.fn({"question_id": bogus, "budget": 5})
    assert "Dispatch rejected" in rejected
    assert state.dispatches == []

    mistyped = page.id[:8] + "-XXXX-XXXX-XXXX-XXXXXXXXXXXX"
    ok = await tool.fn({"question_id": mistyped, "budget": 5})
    assert ok == "Dispatch recorded."
    assert len(state.dispatches) == 1
    assert state.dispatches[0].payload.question_id == page.id


async def test_scope_only_payload_unaffected_by_validation(tmp_db, prioritization_call):
    """ScopeOnlyDispatchPayload's question_id is injected from the trusted
    scope_question_id, not from the LLM — the validator must not run on
    it (would be redundant DB work, and would block dispatches if the
    scope id resolution somehow flickered)."""
    page = await _save_question(tmp_db)
    state = MoveState(prioritization_call, tmp_db)
    tool = DISPATCH_DEFS[CallType.SCOUT_FACTCHECKS].bind(state, scope_question_id=page.id)

    # ScopeOnly payloads hide question_id from the LLM schema, so the
    # tool input doesn't include it. Pass only the schema-visible field.
    result = await tool.fn({"max_rounds": 3, "fruit_threshold": 4})

    assert result == "Dispatch recorded."
    assert len(state.dispatches) == 1
    assert state.dispatches[0].payload.question_id == page.id


@pytest.fixture
def patch_resolve_to_count(mocker, tmp_db):
    """Fixture that wraps resolve_page_id with a call counter."""
    real = tmp_db.resolve_page_id
    counter = {"n": 0}

    async def counting(pid: str):
        counter["n"] += 1
        return await real(pid)

    mocker.patch.object(tmp_db, "resolve_page_id", side_effect=counting)
    return counter


async def test_validation_does_one_db_lookup_on_happy_path(
    tmp_db, prioritization_call, patch_resolve_to_count
):
    """Happy-path full UUID input issues exactly one resolve_page_id call —
    no extra closest-match probe when the primary lookup hits."""
    page = await _save_question(tmp_db)
    state = MoveState(prioritization_call, tmp_db)
    tool = DISPATCH_DEFS[CallType.WEB_RESEARCH].bind(state)

    # Reset counter after page creation (which doesn't itself call resolve).
    patch_resolve_to_count["n"] = 0

    result = await tool.fn({"question_id": page.id})
    assert result == "Dispatch recorded."
    assert patch_resolve_to_count["n"] == 1
