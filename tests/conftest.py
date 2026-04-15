"""Shared fixtures for tests."""

import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

_SKILLS_LIB = Path(__file__).resolve().parent.parent / ".claude" / "lib"
if str(_SKILLS_LIB) not in sys.path:
    sys.path.insert(0, str(_SKILLS_LIB))

from rumil.settings import override_settings
from rumil.database import DB
from rumil.models import (
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
    with override_settings(rumil_test_mode="1"):
        yield


def pytest_addoption(parser):
    parser.addoption(
        "--llm",
        action="store_true",
        default=False,
        help="Run tests that call the real LLM API",
    )
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run slow integration tests (implies --llm)",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "llm: tests that call the real LLM API")
    config.addinivalue_line(
        "markers", "integration: slow integration tests that call the real LLM API"
    )


def pytest_collection_modifyitems(config, items):
    run_llm = config.getoption("--llm")
    run_integration = config.getoption("--integration")
    if run_integration:
        run_llm = True

    for item in items:
        if "integration" in item.keywords and not run_integration:
            item.add_marker(pytest.mark.skip(reason="needs --integration flag to run"))
        elif "llm" in item.keywords and not run_llm:
            item.add_marker(pytest.mark.skip(reason="needs --llm flag to run"))


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
    """Create and return a question page in the DB (TAI-framed so LLM tests don't get refused)."""
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="How quickly will frontier AI automate routine cognitive labour?",
        headline="How quickly will frontier AI automate routine cognitive labour?",
    )
    await tmp_db.save_page(page)
    return page


@pytest_asyncio.fixture
async def scout_call(tmp_db, question_page):
    """Create a pending find_considerations call, saved to DB."""
    call = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    return call


@pytest_asyncio.fixture
async def assess_call(tmp_db, question_page):
    """Create a pending assess call, saved to DB."""
    call = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    return call


@pytest_asyncio.fixture
async def prioritization_call(tmp_db, question_page):
    """Create a pending prioritization call, saved to DB."""
    call = Call(
        call_type=CallType.PRIORITIZATION,
        workspace=Workspace.PRIORITIZATION,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
        budget_allocated=5,
    )
    await tmp_db.save_call(call)
    return call
