"""Regression tests for DB.update_page_content.

update_page_content records a mutation event with event_type='update_page_content'
and, for non-staged runs, also updates the page row directly.

Before the CHECK-constraint-widening migration, the mutation_events CHECK
constraint only accepted 'supersede_page', 'delete_link', 'change_link_role'
and any call to update_page_content would fail with a Postgres APIError
(chaosGuppy/rumil#281).
"""

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest
import pytest_asyncio

from rumil.database import DB
from rumil.models import (
    Page,
    PageLayer,
    PageType,
    Workspace,
)


async def _make_page(db: DB, content: str = "original") -> Page:
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=content,
        headline="test page",
    )
    await db.save_page(page)
    return page


@pytest_asyncio.fixture
async def project_id():
    db = await DB.create(run_id=str(uuid.uuid4()))
    project = await db.get_or_create_project(f"test-upc-{uuid.uuid4().hex[:8]}")
    yield project.id
    await db._execute(db.client.table("projects").delete().eq("id", project.id))


async def _make_db(project_id: str, staged: bool = False) -> DB:
    # Staged DBs in this test file use the pre-fork-at-snapshot loose
    # semantics (baseline visible whenever queried) so that tests which
    # seed baseline data *after* constructing the staged DB continue to
    # work. See tests/test_fork_at_snapshot.py for tests that exercise
    # the pinned-snapshot contract.
    if staged:
        db = await DB.create(
            run_id=str(uuid.uuid4()),
            staged=staged,
            snapshot_ts=datetime.max.replace(tzinfo=UTC),
        )
    else:
        db = await DB.create(run_id=str(uuid.uuid4()), staged=staged)
    db.project_id = project_id
    return db


async def test_update_page_content_non_staged_writes_new_content(project_id):
    """Regression: a non-staged update_page_content must succeed and write
    the new content to the base table. On main (pre-migration), this raises
    a Postgres APIError because the mutation_events CHECK constraint does
    not include 'update_page_content'."""
    db = await _make_db(project_id, staged=False)
    try:
        page = await _make_page(db, content="original content")

        await db.update_page_content(page.id, "replaced content")

        fetched = await db.get_page(page.id)
        assert fetched is not None
        assert fetched.content == "replaced content"
    finally:
        await db.delete_run_data()


async def test_update_page_content_records_mutation_event(project_id):
    """update_page_content must record a mutation event with the old and
    new content in the payload. Locks in the event shape so retroactive
    staging of non-staged runs can replay content updates."""
    db = await _make_db(project_id, staged=False)
    try:
        page = await _make_page(db, content="before")

        await db.update_page_content(page.id, "after")

        rows = (
            await db.client.table("mutation_events")
            .select("event_type, target_id, payload")
            .eq("run_id", db.run_id)
            .eq("target_id", page.id)
            .execute()
        )
        data = cast(list[dict[str, Any]], rows.data)
        events = [r for r in data if r["event_type"] == "update_page_content"]
        assert len(events) == 1
        payload = cast(dict[str, Any], events[0]["payload"])
        assert payload["old_content"] == "before"
        assert payload["new_content"] == "after"
    finally:
        await db.delete_run_data()


async def test_update_page_content_staged_leaves_base_row_alone(project_id):
    """In a staged run, update_page_content must only record the event;
    the base table row keeps its original content. Another (non-staged)
    reader should still see 'original'."""
    baseline = await _make_db(project_id, staged=False)
    staged = await _make_db(project_id, staged=True)
    reader = await _make_db(project_id, staged=False)
    try:
        page = await _make_page(baseline, content="baseline content")

        await staged.update_page_content(page.id, "staged content")

        # Staged run sees its own overlay.
        staged_view = await staged.get_page(page.id)
        assert staged_view is not None
        assert staged_view.content == "staged content"

        # Non-staged reader still sees the original.
        baseline_view = await reader.get_page(page.id)
        assert baseline_view is not None
        assert baseline_view.content == "baseline content"
    finally:
        await staged.delete_run_data()
        await baseline.delete_run_data()
        await reader.delete_run_data()


async def test_update_page_content_on_missing_page_raises(project_id):
    """update_page_content on a nonexistent page raises ValueError."""
    db = await _make_db(project_id, staged=False)
    try:
        fake_id = str(uuid.uuid4())

        with pytest.raises(ValueError, match="not found"):
            await db.update_page_content(fake_id, "irrelevant")
    finally:
        await db.delete_run_data()
