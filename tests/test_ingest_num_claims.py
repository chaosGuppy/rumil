"""Verify that `ingest_num_claims` flows from Settings into the IngestCall task description."""

import types
from typing import cast

from rumil.calls.ingest import IngestCall
from rumil.calls.stages import CallInfra
from rumil.models import Page
from rumil.settings import override_settings


def _make_bare_ingest_call() -> IngestCall:
    """Build an IngestCall without running __init__ (avoids DB/broadcaster plumbing)."""
    call = IngestCall.__new__(IngestCall)
    call._source_page = cast(Page, types.SimpleNamespace(id="src_page_id", extra={}))
    call._filename = "doc.md"
    call.infra = cast(CallInfra, types.SimpleNamespace(question_id="q_abc"))
    return call


def test_task_description_honors_ingest_num_claims():
    call = _make_bare_ingest_call()

    with override_settings(ingest_num_claims=7):
        desc = call.task_description()
    assert "approximately 7 considerations" in desc

    with override_settings(ingest_num_claims=2):
        desc = call.task_description()
    assert "approximately 2 considerations" in desc


def test_task_description_uses_default_when_unset():
    call = _make_bare_ingest_call()
    with override_settings():
        desc = call.task_description()
    assert "approximately 4 considerations" in desc
