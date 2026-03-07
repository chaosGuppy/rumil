"""Shared fixtures for call tests."""
import os

os.environ["DIFFERENTIAL_TEST_MODE"] = "1"

import pytest

from differential.database import DB, init_db
from differential.models import (
    Call,
    CallStatus,
    CallType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)


@pytest.fixture
def tmp_db(tmp_path):
    """Create a fresh SQLite DB in a temp directory."""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    db = DB(db_path)
    db.init_budget(100)
    return db


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
