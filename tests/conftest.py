"""Shared fixtures for call tests."""

import os

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

_TEXT_ID_TABLES = ["page_flags", "page_ratings", "page_links", "calls", "pages"]


@pytest.fixture
def tmp_db():
    """Create a DB using the test schema so production data is untouched."""
    db = DB(schema="test")
    for table in _TEXT_ID_TABLES:
        db.client.table(table).delete().neq("id", "__never__").execute()
    db.client.table("budget").delete().gte("id", 0).execute()
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
