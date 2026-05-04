"""Tests for resolve_page_id's prefix-match fallback on long inputs.

Background: an LLM mistyping a middle UUID segment (e.g.
``9b8836d4-91fc-435f-...`` instead of ``9b8836d4-95e5-41fc-...``) used to
silently miss because resolve_page_id only did exact-match for >8 char
inputs. The fallback resolves these via the leading 8 hex chars when
that prefix is unambiguous.
"""

import pytest

from rumil.models import Page, PageLayer, PageType, Workspace


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


async def test_resolve_recovers_long_input_via_first_eight_chars(tmp_db):
    """A long string whose first 8 chars match an existing page resolves to
    the canonical full UUID, even when the rest of the string is garbage."""
    page = await _save_question(tmp_db)

    # Mangle every char after position 8 — leading prefix preserved.
    mistyped = page.id[:8] + "-XXXX-XXXX-XXXX-XXXXXXXXXXXX"
    assert mistyped != page.id

    resolved = await tmp_db.resolve_page_id(mistyped)
    assert resolved == page.id


async def test_resolve_returns_none_for_long_input_with_no_prefix_match(tmp_db):
    """If even the leading 8 chars don't match anything, no recovery."""
    await _save_question(tmp_db)

    bogus = "deadbeef-0000-0000-0000-000000000000"
    resolved = await tmp_db.resolve_page_id(bogus)
    assert resolved is None


async def test_resolve_exact_match_still_wins(tmp_db):
    """Happy-path full UUID should still hit on exact match (regression)."""
    page = await _save_question(tmp_db)

    resolved = await tmp_db.resolve_page_id(page.id)
    assert resolved == page.id


async def test_resolve_short_id_still_works(tmp_db):
    """8-char short IDs continue to resolve via prefix match (regression)."""
    page = await _save_question(tmp_db)

    resolved = await tmp_db.resolve_page_id(page.id[:8])
    assert resolved == page.id


async def test_resolve_returns_none_on_ambiguous_long_prefix(tmp_db, mocker):
    """If two pages share the leading 8 chars, a long mistyped input resolves
    to None rather than guessing — same ambiguity guard as short IDs."""
    page = await _save_question(tmp_db, headline="q1")

    # Force a colliding-prefix match on the fallback query so we don't
    # depend on the real Page.id generation handing us a collision.
    real_execute = tmp_db._execute
    sibling_id = page.id[:8] + "-FFFF-FFFF-FFFF-FFFFFFFFFFFF"

    async def fake_execute(query):
        result = await real_execute(query)
        # The fallback prefix query is `LIKE '<first8>%'`; it returns rows
        # of {"id": ...}. Append a sibling row with the same prefix.
        rows = getattr(result, "data", None)
        if isinstance(rows, list) and rows and all("id" in r for r in rows):
            if any(r["id"] == page.id for r in rows) and len(rows) == 1:
                rows.append({"id": sibling_id})
        return result

    mocker.patch.object(tmp_db, "_execute", side_effect=fake_execute)

    mistyped = page.id[:8] + "-XXXX-XXXX-XXXX-XXXXXXXXXXXX"
    resolved = await tmp_db.resolve_page_id(mistyped)
    assert resolved is None


async def test_resolve_url_branch_still_reachable(tmp_db, mocker):
    """A URL-shaped >8 char input must still hit the URL match branch,
    not get short-circuited by the new prefix fallback."""
    page = Page(
        page_type=PageType.SOURCE,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="source body",
        headline="source",
        extra={"url": "https://example.com/article"},
    )
    await tmp_db.save_page(page)

    resolved = await tmp_db.resolve_page_id("https://example.com/article")
    assert resolved == page.id


@pytest.fixture
async def two_pages(tmp_db):
    a = await _save_question(tmp_db, headline="a")
    b = await _save_question(tmp_db, headline="b")
    return a, b


async def test_resolve_does_not_misroute_between_distinct_pages(two_pages, tmp_db):
    """Two unrelated pages: each long ID must round-trip to itself, never to
    the other. The prefix fallback should only fire on exact-match misses."""
    a, b = two_pages
    assert await tmp_db.resolve_page_id(a.id) == a.id
    assert await tmp_db.resolve_page_id(b.id) == b.id
