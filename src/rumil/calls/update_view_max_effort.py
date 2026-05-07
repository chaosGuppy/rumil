"""Max-effort variant of update_view: same logic, but the ContextBuilder is
wrapped with the impact-percentile filter."""

from rumil.calls.impact_filtered_context import ImpactFilteredContext
from rumil.calls.stages import ContextBuilder
from rumil.calls.update_view import UpdateViewCall, UpdateViewContext
from rumil.models import CallType


class UpdateViewMaxEffortCall(UpdateViewCall):
    """Incrementally update a View using the impact-percentile filter on top
    of the standard update_view context.

    Reuses UpdateViewCall's WorkspaceUpdater (phase pipeline) and
    ClosingReviewer (view scoring) — only the context phase differs.
    """

    call_type = CallType.UPDATE_VIEW_MAX_EFFORT

    def _make_context_builder(self) -> ContextBuilder:
        return ImpactFilteredContext(inner_builder=UpdateViewContext())
