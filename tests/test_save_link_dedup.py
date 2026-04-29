"""Tests for the save_link first-write-wins dedup.

The DB has no uniqueness index on (from, to, link_type, direction) yet —
save_link is the only guard against accidental duplicates from concurrent
or repeated callers. These tests pin the contract: same edge identity =>
no second row, regardless of the new PageLink's id or metadata.
"""

import uuid

import pytest
import pytest_asyncio

from rumil.database import DB
from rumil.models import (
    ConsiderationDirection,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)


def _claim(headline: str = "claim") -> Page:
    return Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
    )


def _question(headline: str = "question") -> Page:
    return Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
    )


@pytest_asyncio.fixture
async def src_and_dst(tmp_db) -> tuple[Page, Page]:
    src = _claim("src claim")
    dst = _question("dst question")
    await tmp_db.save_page(src)
    await tmp_db.save_page(dst)
    return src, dst


async def _count_rows_between(
    db: DB,
    from_id: str,
    to_id: str,
    link_type: LinkType,
) -> int:
    """Count *baseline* rows directly, bypassing the staged-run overlay so
    we can verify the DB-level row count rather than the visible view."""
    rows = (
        await db._execute(
            db.client.table("page_links")
            .select("id")
            .eq("from_page_id", from_id)
            .eq("to_page_id", to_id)
            .eq("link_type", link_type.value)
        )
    ).data
    return len(rows)


async def test_save_link_dedups_second_call_with_fresh_id(tmp_db, src_and_dst):
    """Two save_link calls for the same (from, to, link_type, direction)
    with different ids — only one row written."""
    src, dst = src_and_dst
    first = PageLink(
        from_page_id=src.id,
        to_page_id=dst.id,
        link_type=LinkType.CONSIDERATION,
        direction=ConsiderationDirection.SUPPORTS,
    )
    second = PageLink(
        from_page_id=src.id,
        to_page_id=dst.id,
        link_type=LinkType.CONSIDERATION,
        direction=ConsiderationDirection.SUPPORTS,
        reasoning="different reasoning",
    )
    assert first.id != second.id

    await tmp_db.save_link(first)
    await tmp_db.save_link(second)

    assert await _count_rows_between(tmp_db, src.id, dst.id, LinkType.CONSIDERATION) == 1
    # First-write-wins: original metadata preserved.
    surviving = (await tmp_db.get_links_from(src.id))[0]
    assert surviving.id == first.id


async def test_save_link_does_not_dedup_distinct_directions(tmp_db, src_and_dst):
    """Same (from, to, link_type) but different direction — both kept."""
    src, dst = src_and_dst
    supports = PageLink(
        from_page_id=src.id,
        to_page_id=dst.id,
        link_type=LinkType.CONSIDERATION,
        direction=ConsiderationDirection.SUPPORTS,
    )
    opposes = PageLink(
        from_page_id=src.id,
        to_page_id=dst.id,
        link_type=LinkType.CONSIDERATION,
        direction=ConsiderationDirection.OPPOSES,
    )
    await tmp_db.save_link(supports)
    await tmp_db.save_link(opposes)

    assert await _count_rows_between(tmp_db, src.id, dst.id, LinkType.CONSIDERATION) == 2


async def test_save_link_does_not_dedup_distinct_link_types(tmp_db, src_and_dst):
    """Same (from, to) but different link_type — both kept."""
    src, dst = src_and_dst
    consid = PageLink(
        from_page_id=src.id,
        to_page_id=dst.id,
        link_type=LinkType.CONSIDERATION,
        direction=ConsiderationDirection.SUPPORTS,
    )
    related = PageLink(
        from_page_id=src.id,
        to_page_id=dst.id,
        link_type=LinkType.RELATED,
    )
    await tmp_db.save_link(consid)
    await tmp_db.save_link(related)

    assert await _count_rows_between(tmp_db, src.id, dst.id, LinkType.CONSIDERATION) == 1
    assert await _count_rows_between(tmp_db, src.id, dst.id, LinkType.RELATED) == 1


async def test_save_link_re_save_by_same_id_is_upsert(tmp_db, src_and_dst):
    """Saving the same link object again (same id) goes through the upsert
    path — used by callers that want to update an existing link's
    metadata."""
    src, dst = src_and_dst
    link = PageLink(
        from_page_id=src.id,
        to_page_id=dst.id,
        link_type=LinkType.RELATED,
        reasoning="initial",
    )
    await tmp_db.save_link(link)

    link.reasoning = "updated"
    await tmp_db.save_link(link)

    assert await _count_rows_between(tmp_db, src.id, dst.id, LinkType.RELATED) == 1
    surviving = (await tmp_db.get_links_from(src.id))[0]
    assert surviving.reasoning == "updated"


async def test_save_link_dedups_against_null_direction(tmp_db, src_and_dst):
    """Two RELATED links between the same pages — both have direction=None,
    second call deduped."""
    src, dst = src_and_dst
    first = PageLink(from_page_id=src.id, to_page_id=dst.id, link_type=LinkType.RELATED)
    second = PageLink(from_page_id=src.id, to_page_id=dst.id, link_type=LinkType.RELATED)
    assert first.direction is None and second.direction is None
    assert first.id != second.id

    await tmp_db.save_link(first)
    await tmp_db.save_link(second)

    assert await _count_rows_between(tmp_db, src.id, dst.id, LinkType.RELATED) == 1


async def test_save_link_staged_run_dedups_against_baseline(tmp_db, src_and_dst):
    """A staged DB sharing baseline pages should see the baseline link
    via _staged_filter and not insert a duplicate staged row."""
    src, dst = src_and_dst
    baseline_link = PageLink(
        from_page_id=src.id,
        to_page_id=dst.id,
        link_type=LinkType.CONSIDERATION,
        direction=ConsiderationDirection.SUPPORTS,
    )
    await tmp_db.save_link(baseline_link)

    staged = await DB.create(run_id=str(uuid.uuid4()), staged=True)
    staged.project_id = tmp_db.project_id
    try:
        duplicate = PageLink(
            from_page_id=src.id,
            to_page_id=dst.id,
            link_type=LinkType.CONSIDERATION,
            direction=ConsiderationDirection.SUPPORTS,
        )
        await staged.save_link(duplicate)

        # Only one row exists overall — the baseline one.
        assert await _count_rows_between(tmp_db, src.id, dst.id, LinkType.CONSIDERATION) == 1
        # And the staged run sees that same id when it queries.
        staged_links = await staged.get_links_from(src.id)
        assert len(staged_links) == 1
        assert staged_links[0].id == baseline_link.id
    finally:
        await staged.delete_run_data()
        await staged.close()


async def test_save_link_independent_staged_runs_each_can_create(tmp_db, src_and_dst):
    """Two staged runs each save 'their' version of the same edge. Each is
    invisible to the other, so neither dedups against the other; both
    insert. (At commit time the unique-index migration would catch this —
    that's the next layer.)"""
    src, dst = src_and_dst

    run_a = await DB.create(run_id=str(uuid.uuid4()), staged=True)
    run_a.project_id = tmp_db.project_id
    run_b = await DB.create(run_id=str(uuid.uuid4()), staged=True)
    run_b.project_id = tmp_db.project_id
    try:
        await run_a.save_link(
            PageLink(
                from_page_id=src.id,
                to_page_id=dst.id,
                link_type=LinkType.CONSIDERATION,
                direction=ConsiderationDirection.SUPPORTS,
            )
        )
        await run_b.save_link(
            PageLink(
                from_page_id=src.id,
                to_page_id=dst.id,
                link_type=LinkType.CONSIDERATION,
                direction=ConsiderationDirection.SUPPORTS,
            )
        )
        assert await _count_rows_between(tmp_db, src.id, dst.id, LinkType.CONSIDERATION) == 2

        # But within a single staged run, dedup still applies.
        await run_a.save_link(
            PageLink(
                from_page_id=src.id,
                to_page_id=dst.id,
                link_type=LinkType.CONSIDERATION,
                direction=ConsiderationDirection.SUPPORTS,
            )
        )
        assert await _count_rows_between(tmp_db, src.id, dst.id, LinkType.CONSIDERATION) == 2
    finally:
        await run_a.delete_run_data()
        await run_a.close()
        await run_b.delete_run_data()
        await run_b.close()


@pytest.mark.asyncio
async def test_save_link_dedups_repeated_sequential_calls(tmp_db, src_and_dst):
    """Many sequential save_link calls for the same edge collapse to one
    row. (Concurrent callers can still race past the application check —
    a DB-level partial unique index is the planned next layer.)"""
    src, dst = src_and_dst

    for _ in range(8):
        await tmp_db.save_link(
            PageLink(
                from_page_id=src.id,
                to_page_id=dst.id,
                link_type=LinkType.CONSIDERATION,
                direction=ConsiderationDirection.SUPPORTS,
            )
        )

    assert await _count_rows_between(tmp_db, src.id, dst.id, LinkType.CONSIDERATION) == 1
