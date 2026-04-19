"""Mid-run human steering.

Typed nudges authored via parma chat, inline UI, CLI, and the /rumil-nudge
skill, consumed by the orchestrator at dispatch boundaries and by every
call at context-build time.

Public surface:
  * ``filter_dispatch_sequences`` — apply hard nudges to prioritization output.
  * ``render_steering_context`` — soft-concat NL nudges for context injection.
  * ``build_applied_event`` — helper for emitting ``NudgeAppliedEvent``.
  * ``consume_one_shot`` — flip status after a nudge fires on a batch / call.
"""

from rumil.nudges.consumer import (
    build_applied_event,
    consume_one_shot,
    filter_dispatch_sequences,
    render_steering_context,
)

__all__ = [
    "build_applied_event",
    "consume_one_shot",
    "filter_dispatch_sequences",
    "render_steering_context",
]
