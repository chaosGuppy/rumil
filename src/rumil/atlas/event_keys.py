"""Single source of truth for trace-event discriminator strings atlas
reads from ``trace_json``.

Every other atlas module that filters trace events by ``event == "..."``
should import these constants instead of inlining the literal. The
constants are pulled directly from each event class's ``event``
discriminator at import time, so a rename in ``trace_events.py`` shows
up either as an import error here or as a drift-test failure rather
than a silent miscount in the readers.

The companion test in ``tests/test_atlas_descriptions.py`` walks every
non-deprecated event class and asserts its discriminator value
appears either in this module's exports or on a known allowlist of
events atlas doesn't (yet) read.
"""

from __future__ import annotations

from rumil.tracing.trace_events import (
    ContextBuiltEvent,
    DispatchesPlannedEvent,
    DispatchExecutedEvent,
    ErrorEvent,
    LLMExchangeEvent,
    LoadPageEvent,
    MovesExecutedEvent,
    PhaseSkippedEvent,
    ReviewCompleteEvent,
    ScoringCompletedEvent,
    ViewCreatedEvent,
    WarningEvent,
)


def _lit(cls: type) -> str:
    """Pull the ``event`` discriminator value from an event class.

    Equivalent to inspecting the ``Literal[...]`` default; we use
    ``model_fields`` because Pydantic v2 stores the default there and
    ``cls.event`` would be the Literal alias type, not the string.
    """
    field = cls.model_fields["event"]  # pyright: ignore[reportAttributeAccessIssue]
    return str(field.default)


CONTEXT_BUILT = _lit(ContextBuiltEvent)
MOVES_EXECUTED = _lit(MovesExecutedEvent)
REVIEW_COMPLETE = _lit(ReviewCompleteEvent)
LLM_EXCHANGE = _lit(LLMExchangeEvent)
WARNING = _lit(WarningEvent)
ERROR = _lit(ErrorEvent)
SCORING_COMPLETED = _lit(ScoringCompletedEvent)
DISPATCHES_PLANNED = _lit(DispatchesPlannedEvent)
DISPATCH_EXECUTED = _lit(DispatchExecutedEvent)
LOAD_PAGE = _lit(LoadPageEvent)
VIEW_CREATED = _lit(ViewCreatedEvent)
PHASE_SKIPPED = _lit(PhaseSkippedEvent)


# Every literal atlas reads. The drift-prevention test asserts each
# class's discriminator matches the constant, so renames break loudly.
ATLAS_READS: dict[str, type] = {
    CONTEXT_BUILT: ContextBuiltEvent,
    MOVES_EXECUTED: MovesExecutedEvent,
    REVIEW_COMPLETE: ReviewCompleteEvent,
    LLM_EXCHANGE: LLMExchangeEvent,
    WARNING: WarningEvent,
    ERROR: ErrorEvent,
    SCORING_COMPLETED: ScoringCompletedEvent,
    DISPATCHES_PLANNED: DispatchesPlannedEvent,
    DISPATCH_EXECUTED: DispatchExecutedEvent,
    LOAD_PAGE: LoadPageEvent,
    VIEW_CREATED: ViewCreatedEvent,
    PHASE_SKIPPED: PhaseSkippedEvent,
}
