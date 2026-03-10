"""Shared fixtures for tests."""

import os
import uuid

from dotenv import load_dotenv

load_dotenv()
os.environ["DIFFERENTIAL_TEST_MODE"] = "1"

import pytest

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


@pytest.fixture
def tmp_db():
    """Create a DB with a unique run_id for test isolation."""
    run_id = str(uuid.uuid4())
    db = DB(run_id=run_id)
    project = db.get_or_create_project("default")
    db.project_id = project.id
    db.init_budget(100)
    yield db
    db.delete_run_data()


@pytest.fixture
def question_page(tmp_db):
    """Create and return a question page in the DB."""
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Is the sky blue?",
        summary="Is the sky blue?",
    )
    tmp_db.save_page(page)
    return page


@pytest.fixture
def scout_call(question_page):
    """Create a pending scout call (not yet saved to DB)."""
    return Call(
        call_type=CallType.SCOUT,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )


@pytest.fixture
def assess_call(question_page):
    """Create a pending assess call (not yet saved to DB)."""
    return Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )


@pytest.fixture
def prioritization_call(question_page):
    """Create a pending prioritization call (not yet saved to DB)."""
    return Call(
        call_type=CallType.PRIORITIZATION,
        workspace=Workspace.PRIORITIZATION,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
        budget_allocated=5,
    )
