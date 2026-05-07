"""SimpleSpine trace events.

The Pydantic event models live in :mod:`rumil.tracing.trace_events` so
they can join the canonical ``TraceEvent`` discriminated union (which
``CallTrace.record`` accepts). This module re-exports them so the rest
of the SimpleSpine package can import them locally.
"""

from rumil.tracing.trace_events import (
    SpineConfigPrepEvent,
    SpineFinalizedEvent,
    SpineNoteAddedEvent,
    SpineRoundStartedEvent,
    SpineSpawnCompletedEvent,
    SpineSpawnStartedEvent,
    SpineThrottledEvent,
)

__all__ = (
    "SpineConfigPrepEvent",
    "SpineFinalizedEvent",
    "SpineNoteAddedEvent",
    "SpineRoundStartedEvent",
    "SpineSpawnCompletedEvent",
    "SpineSpawnStartedEvent",
    "SpineThrottledEvent",
)
