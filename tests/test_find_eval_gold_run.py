"""Tests for DB.find_eval_gold_run gold-run lookup."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest_asyncio

from rumil.database import DB

SeedFn = Callable[..., Awaitable[str]]


@pytest_asyncio.fixture
async def seed_run(tmp_db) -> AsyncIterator[SeedFn]:
    """Yield a helper that creates eval-tagged run rows in tmp_db's project,
    tracking each run_id so the rows can be deleted on teardown.

    The default cleanup of `delete_run_data(delete_project=True)` only
    targets tmp_db.run_id; rows created under different run_ids would
    otherwise block the project deletion via the runs.project_id FK.
    """
    seeded: list[str] = []

    async def _seed(
        *,
        question_id: str,
        role: str,
        builder: str,
        name: str = "seed",
        project_id: str | None = None,
    ) -> str:
        run_id = str(uuid.uuid4())
        rdb = await DB.create(run_id=run_id, staged=False)
        rdb.project_id = project_id or tmp_db.project_id
        try:
            config = {
                "eval": {
                    "role": role,
                    "context_builder": builder,
                    "paired_run_id": None,
                    "question_id": question_id,
                }
            }
            await rdb.create_run(name=name, question_id=question_id, config=config)
        finally:
            await rdb.close()
        seeded.append(run_id)
        return run_id

    yield _seed

    cleanup = await DB.create(run_id=str(uuid.uuid4()), staged=False)
    try:
        for run_id in seeded:
            await cleanup._execute(cleanup.client.table("runs").delete().eq("id", run_id))
    finally:
        await cleanup.close()


async def test_find_eval_gold_run_returns_none_when_no_gold(
    tmp_db,
    question_page,
):
    assert await tmp_db.find_eval_gold_run(question_page.id) is None


async def test_find_eval_gold_run_finds_gold_run(
    tmp_db,
    question_page,
    seed_run,
):
    gold_run_id = await seed_run(
        question_id=question_page.id,
        role="gold",
        builder="ImpactFilteredContext",
    )
    assert await tmp_db.find_eval_gold_run(question_page.id) == gold_run_id


async def test_find_eval_gold_run_ignores_candidate_runs(
    tmp_db,
    question_page,
    seed_run,
):
    await seed_run(
        question_id=question_page.id,
        role="candidate",
        builder="ImpactFilteredContext",
    )
    assert await tmp_db.find_eval_gold_run(question_page.id) is None


async def test_find_eval_gold_run_picks_most_recent_gold(
    tmp_db,
    question_page,
    seed_run,
):
    older = await seed_run(
        question_id=question_page.id,
        role="gold",
        builder="ImpactFilteredContext",
        name="older",
    )
    await asyncio.sleep(1.1)
    newer = await seed_run(
        question_id=question_page.id,
        role="gold",
        builder="ImpactFilteredContext",
        name="newer",
    )
    found = await tmp_db.find_eval_gold_run(question_page.id)
    assert found == newer
    assert found != older


async def test_find_eval_gold_run_filters_by_builder_name(
    tmp_db,
    question_page,
    seed_run,
):
    await seed_run(
        question_id=question_page.id,
        role="gold",
        builder="EmbeddingContext",
    )
    # Default lookup is for ImpactFilteredContext, which doesn't exist here.
    assert await tmp_db.find_eval_gold_run(question_page.id) is None
    # But a lookup for the actual builder should hit.
    assert await tmp_db.find_eval_gold_run(question_page.id, "EmbeddingContext") is not None


async def test_find_eval_gold_run_isolates_projects(
    tmp_db,
    question_page,
    seed_run,
):
    other_db = await DB.create(run_id=str(uuid.uuid4()), staged=False)
    other_project = await other_db.get_or_create_project(f"other-{uuid.uuid4().hex[:6]}")
    await other_db.close()

    try:
        await seed_run(
            question_id=question_page.id,
            role="gold",
            builder="ImpactFilteredContext",
            project_id=other_project.id,
        )
        # tmp_db's project still has no gold of its own.
        assert await tmp_db.find_eval_gold_run(question_page.id) is None
    finally:
        cleanup = await DB.create(run_id=str(uuid.uuid4()), staged=False)
        try:
            # Delete the run rows first so the FK on runs.project_id releases.
            await cleanup._execute(
                cleanup.client.table("runs").delete().eq("project_id", str(other_project.id))
            )
            await cleanup._execute(
                cleanup.client.table("projects").delete().eq("id", str(other_project.id))
            )
        finally:
            await cleanup.close()
