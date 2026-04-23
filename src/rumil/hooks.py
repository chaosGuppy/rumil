"""Default workspace lifecycle hooks.

These are opt-in event handlers registered at CLI/API startup. They keep the
event bus (`rumil.events`) general-purpose — concrete behaviour lives here.
"""

from __future__ import annotations

import logging

from rumil.events import PageCreatedEvent, register
from rumil.models import PageType
from rumil.settings import get_settings

log = logging.getLogger(__name__)


async def auto_create_view_on_question(event: PageCreatedEvent) -> None:
    """Run CreateView on a freshly-created question so every question has an
    early synthesis artefact. Short-circuits unless the event is a question,
    the feature flag is on, the event carries a DB, and no View already exists."""
    if event.page_type != PageType.QUESTION:
        return
    if not get_settings().auto_create_view_on_question:
        return
    if event.db is None:
        return

    from rumil.orchestrators.common import create_view_for_question

    existing = await event.db.get_view_for_question(event.page_id)
    if existing is not None:
        return
    await create_view_for_question(event.page_id, event.db)


def register_default_hooks() -> None:
    """Wire default handlers onto the module-level event bus.

    Called from CLI and API entry points. Idempotent at the handler level
    only if callers avoid double-registration — we do not dedupe here so
    a double-call would genuinely register the handler twice.
    """
    register(PageCreatedEvent, auto_create_view_on_question)
