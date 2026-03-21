"""Find considerations call: find missing considerations on a question."""

import logging

from rumil.calls.closing_reviewers import SinglePhaseScoutReview, TwoPhaseScoutReview
from rumil.calls.context_builders import ScoutEmbeddingContext, FindConsiderationsGraphContext
from rumil.calls.page_creators import MultiRoundLoop
from rumil.calls.stages import CallRunner, ClosingReviewer, ContextBuilder, PageCreator
from rumil.context import format_page
from rumil.database import DB
from rumil.llm import build_system_prompt, build_user_message
from rumil.models import Call, CallStage, CallType, MoveType, PageDetail, FindConsiderationsMode
from rumil.moves.base import MoveState
from rumil.moves.registry import MOVES
from rumil.page_graph import PageGraph
from rumil.tracing.tracer import CallTrace
from rumil.workspace_map import build_workspace_map

log = logging.getLogger(__name__)


_LINKING_TASK = (
    'Review the workspace and link relevant existing pages to the scope question.\n\n'
    'For each link, specify a role:\n'
    '- **direct**: "Now I know X, I can immediately update my answer." The page '
    'directly bears on the answer to the scope question \u2014 it is evidence, a '
    'counter-argument, or a partial answer.\n'
    '- **structural**: "Now I know X, I know better what evidence and angles to '
    'consider." The page frames the investigation \u2014 it indicates what to look '
    'for, how to decompose the question, or what dimensions matter.\n\n'
    'Link claims as considerations and sub-questions as child questions.\n\n'
    'Be discerning. Only link pages that genuinely bear on this question \u2014 '
    'tangential or weakly related pages should not be linked. '
    'Do not duplicate any links already shown above. '
    'Create no more than 6 new links, and fewer if fewer are warranted \u2014 '
    'do not force links just to fill a quota.\n\n'
    'Scope question ID: `{question_id}`'
)


async def link_new_pages(
    question_id: str,
    call: Call,
    db: DB,
    state: MoveState,
    trace: CallTrace,
    context_page_ids: list[str] | None = None,
    graph: PageGraph | None = None,
) -> None:
    """Single LLM call that reviews nearby pages and creates direct/structural links.

    Uses only LINK_CONSIDERATION and LINK_CHILD_QUESTION tools with role fields.
    Free (not counted against budget).

    If *context_page_ids* is provided, those pages are shown as headlines
    instead of the full workspace map.
    """
    from rumil.calls.common import run_single_call

    source: DB | PageGraph = graph if graph is not None else db
    question = await source.get_page(question_id)
    if not question:
        return

    if context_page_ids:
        page_lines: list[str] = []
        for pid in context_page_ids:
            page = await source.get_page(pid)
            if page and page.id != question_id:
                page_lines.append(await format_page(page, PageDetail.HEADLINE))
        pages_text = '# Nearby Pages\n\n' + '\n'.join(page_lines)
    else:
        pages_text, _ = await build_workspace_map(db, graph=graph)

    question_text = await format_page(question, PageDetail.HEADLINE)
    existing_links = await _build_link_inventory(question_id, db, graph=graph)
    working_context = (
        pages_text + '\n\n---\n\n'
        '# Scope Question\n\n' + question_text
        + '\n\n' + existing_links
    )

    linking_tools = [
        MOVES[MoveType.LINK_CONSIDERATION].bind(state),
        MOVES[MoveType.LINK_CHILD_QUESTION].bind(state),
    ]
    task = _LINKING_TASK.format(question_id=question_id)
    system_prompt = build_system_prompt(CallType.FIND_CONSIDERATIONS.value)
    user_message = build_user_message(working_context, task)

    await run_single_call(
        system_prompt=system_prompt,
        user_message=user_message,
        tools=linking_tools,
        call_id=call.id,
        phase='link_new_pages',
        db=db,
        state=state,
        trace=trace,
    )


async def _build_link_inventory(
    question_id: str,
    db: DB,
    graph: PageGraph | None = None,
) -> str:
    source: DB | PageGraph = graph if graph is not None else db
    considerations = await source.get_considerations_for_question(question_id)
    children_with_links = await source.get_child_questions_with_links(question_id)

    if not considerations and not children_with_links:
        return 'No existing links on the scope question.'

    lines = ['### Current Links']
    for page, link in considerations:
        lines.append(
            f'- [{link.role.value}] consideration: '
            f'"{page.headline}" '
            f'(strength {link.strength:.1f}, link_id: `{link.id}`)'
        )
    for page, link in children_with_links:
        lines.append(
            f'- [{link.role.value}] child_question: '
            f'"{page.headline}" '
            f'(link_id: `{link.id}`)'
        )
    return '\n'.join(lines)


class FindConsiderationsCall(CallRunner):
    """Multi-round scout session with fruit checking."""

    context_builder_cls = FindConsiderationsGraphContext
    page_creator_cls = MultiRoundLoop
    closing_reviewer_cls = TwoPhaseScoutReview
    call_type = CallType.FIND_CONSIDERATIONS

    def __init__(
        self,
        question_id: str,
        call: Call,
        db: DB,
        *,
        max_rounds: int,
        fruit_threshold: int,
        mode: FindConsiderationsMode = FindConsiderationsMode.ALTERNATE,
        context_page_ids: list[str] | None = None,
        broadcaster=None,
        up_to_stage: CallStage | None = None,
    ):
        call.call_params = {
            'mode': mode.value,
            'max_rounds': max_rounds,
            'fruit_threshold': fruit_threshold,
        }
        self._mode = mode
        self._max_rounds = max_rounds
        self._fruit_threshold = fruit_threshold
        self._context_page_ids = context_page_ids
        super().__init__(question_id, call, db, broadcaster=broadcaster, up_to_stage=up_to_stage)

    @property
    def rounds_completed(self) -> int:
        if self.creation_result is not None:
            return self.creation_result.rounds_completed
        return 0

    def _make_context_builder(self) -> ContextBuilder:
        return FindConsiderationsGraphContext(self._mode, self._context_page_ids)

    def _make_page_creator(self) -> PageCreator:
        return MultiRoundLoop(
            self._max_rounds, self._fruit_threshold, self._mode,
        )

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return TwoPhaseScoutReview()

    def task_description(self) -> str:
        return (
            'Scout for missing considerations on this question.\n\n'
            f'Question ID (use this when linking considerations): '
            f'`{self.infra.question_id}`'
        )


class EmbeddingFindConsiderationsCall(FindConsiderationsCall):
    """Scout call that builds context via embedding similarity search."""

    context_builder_cls = ScoutEmbeddingContext
    closing_reviewer_cls = SinglePhaseScoutReview

    def _make_context_builder(self) -> ContextBuilder:
        return ScoutEmbeddingContext(self._mode)

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return SinglePhaseScoutReview()
