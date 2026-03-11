"""Shared fixtures for tests."""

import uuid

import pytest
import pytest_asyncio

from differential.settings import override_settings
from differential.database import DB
from differential.models import (
    Call,
    CallStatus,
    CallType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)


@pytest.fixture(autouse=True, scope="session")
def _test_settings():
    """Activate test-mode settings for the entire test session."""
    with override_settings(differential_test_mode="1"):
        yield


def pytest_addoption(parser):
    parser.addoption("--llm", action="store_true", default=False, help="Run tests that call the real LLM API")


def pytest_configure(config):
    config.addinivalue_line("markers", "llm: tests that call the real LLM API")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--llm"):
        return
    skip_llm = pytest.mark.skip(reason="needs --llm flag to run")
    for item in items:
        if "llm" in item.keywords:
            item.add_marker(skip_llm)


@pytest_asyncio.fixture
async def tmp_db():
    """Create a DB with a unique run_id and throwaway project for test isolation."""
    run_id = str(uuid.uuid4())
    db = await DB.create(run_id=run_id)
    project = await db.get_or_create_project(f"test-{run_id[:8]}")
    db.project_id = project.id
    await db.init_budget(100)
    yield db
    await db.delete_run_data(delete_project=True)


@pytest_asyncio.fixture
async def question_page(tmp_db):
    """Create and return a question page in the DB."""
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Is the sky blue?",
        summary="Is the sky blue?",
    )
    await tmp_db.save_page(page)
    return page


@pytest_asyncio.fixture
async def scout_call(question_page):
    """Create a pending scout call (not yet saved to DB)."""
    return Call(
        call_type=CallType.SCOUT,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )


@pytest_asyncio.fixture
async def assess_call(question_page):
    """Create a pending assess call (not yet saved to DB)."""
    return Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )


@pytest_asyncio.fixture
async def prioritization_call(question_page):
    """Create a pending prioritization call (not yet saved to DB)."""
    return Call(
        call_type=CallType.PRIORITIZATION,
        workspace=Workspace.PRIORITIZATION,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
        budget_allocated=5,
    )
