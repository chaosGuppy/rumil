"""Contextvar-based tag stack for page load tracking.

Tags set at higher levels (call type, phase) automatically apply to all
nested ``format_page`` calls.  Scopes nest and merge — inner scopes
inherit outer tags and may override them.

Usage::

    with page_track_scope(call_type="find_considerations"):
        with page_track_scope(phase="context_build"):
            with page_track_scope(source="embedding_full"):
                await format_page(page, PageDetail.CONTENT, track=True)
                # recorded tags include all three levels
"""

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator

_page_track_tags_var: ContextVar[dict[str, str]] = ContextVar(
    "page_track_tags", default={}
)


def get_page_track_tags() -> dict[str, str]:
    """Return the current ambient page-tracking tags."""
    return _page_track_tags_var.get()


@contextmanager
def page_track_scope(**tags: str) -> Iterator[None]:
    """Push tags onto the ambient tracking context.

    Scopes nest: inner scopes see the union of all enclosing tags, with
    inner values overriding outer ones for the same key.  Tags are
    restored when the scope exits.
    """
    current = _page_track_tags_var.get()
    merged = {**current, **tags}
    token = _page_track_tags_var.set(merged)
    try:
        yield
    finally:
        _page_track_tags_var.reset(token)
