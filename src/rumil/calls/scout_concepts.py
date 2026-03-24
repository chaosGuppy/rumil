"""Scout Concepts call: identify concept proposals from the research workspace."""

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import ConceptScoutContext
from rumil.calls.page_creators import SimpleAgentLoop
from rumil.calls.stages import CallRunner, ClosingReviewer, ContextBuilder, PageCreator
from rumil.models import CallType, MoveType


class ScoutConceptsCall(CallRunner):
    """Survey the research workspace and propose concepts for assessment."""

    context_builder_cls = ConceptScoutContext
    page_creator_cls = SimpleAgentLoop
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.SCOUT_CONCEPTS
    available_moves = [MoveType.PROPOSE_CONCEPT, MoveType.LOAD_PAGE]

    def _make_context_builder(self) -> ContextBuilder:
        return ConceptScoutContext(self.call_type)

    def _make_page_creator(self) -> PageCreator:
        return SimpleAgentLoop(
            self.call_type,
            self.task_description(),
            available_moves=self._resolve_available_moves(),
        )

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return StandardClosingReview(self.call_type)

    def task_description(self) -> str:
        return (
            "Survey the research workspace and the concept registry above. "
            "Identify 1-3 concepts or distinctions that would meaningfully "
            "clarify the investigation. Use `propose_concept` to record each proposal."
        )
