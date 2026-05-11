"""Max-effort variant of create_view: same logic, but the ContextBuilder is
wrapped with the impact-percentile filter."""

from rumil.calls.context_builders import CreateViewContext
from rumil.calls.create_view import CreateViewCall
from rumil.calls.impact_filtered_context import ImpactFilteredContext
from rumil.calls.stages import ContextBuilder
from rumil.models import CallType


class CreateViewMaxEffortCall(CreateViewCall):
    """Create a View page using the impact-percentile filter on top of the
    standard create_view context.

    Reuses CreateViewCall's WorkspaceUpdater (item creation) and
    ClosingReviewer (view scoring) — only the context phase differs.
    """

    call_type = CallType.CREATE_VIEW_MAX_EFFORT

    def _make_context_builder(self) -> ContextBuilder:
        return ImpactFilteredContext(inner_builder=CreateViewContext())
