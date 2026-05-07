"""ContextVar-based marker for memo-pipeline runs.

Set by MemoOrchestrator and checked at a few specific seams so the memo
pipeline can opt out of behaviours that don't pay their way for memos:

- StandardClosingReview skips the review LLM call. Memo runs don't feed
  into the broader research-loop confidence machinery, so per-page ratings
  and confidence scores are wasted work.
- CritiqueContext skips its workspace embedding sweep and uses the
  scanner-supplied source pages instead. The scanner already picked
  which pages bear on the candidate; rebuilding embedding context per
  critique cycle is a duplicate of work the scanner did up-front.

Default behaviour is unchanged: callers outside the memo pipeline see no
ContextVar set and behave exactly as before.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_MEMO_SOURCE_PAGES: ContextVar[str | None] = ContextVar(
    "rumil_memo_source_pages",
    default=None,
)


@contextmanager
def memo_mode(*, source_pages_text: str = "") -> Iterator[None]:
    """Mark the current async context as a memo-pipeline run.

    *source_pages_text* is the rendered block of pages the scanner flagged
    as relevant to the memo candidate. CritiqueContext substitutes this
    for its embedding sweep when memo mode is active.
    """
    token = _MEMO_SOURCE_PAGES.set(source_pages_text)
    try:
        yield
    finally:
        _MEMO_SOURCE_PAGES.reset(token)


def is_memo_mode() -> bool:
    """True iff the current async context is inside a memo run."""
    return _MEMO_SOURCE_PAGES.get() is not None


def memo_source_pages() -> str:
    """Source-pages block for the current memo run, or empty if not in memo mode."""
    val = _MEMO_SOURCE_PAGES.get()
    return val if val is not None else ""
