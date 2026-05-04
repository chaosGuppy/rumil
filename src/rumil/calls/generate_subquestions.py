"""Generate Subquestions call: a single structured LLM call producing
a list of subquestions for the scope question, then manual page creation
via existing scout-question machinery."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from rumil.calls.common import (
    mark_call_completed,
    moves_to_trace_event,
)
from rumil.calls.context_builders import EmbeddingContext
from rumil.calls.stages import (
    CallInfra,
    CallRunner,
    ClosingReviewer,
    ContextBuilder,
    ContextResult,
    UpdateResult,
    WorkspaceUpdater,
)
from rumil.llm import (
    LLMExchangeMetadata,
    build_system_prompt,
    build_user_message,
    structured_call,
)
from rumil.models import CallType, Move, MoveType
from rumil.moves.base import HEADLINE_DESCRIPTION
from rumil.moves.create_question import (
    QUESTION_CONTENT_DESCRIPTION,
    CreateScoutQuestionPayload,
    execute_scout_question,
)

log = logging.getLogger(__name__)


class _GeneratedSubquestion(BaseModel):
    headline: str = Field(description=HEADLINE_DESCRIPTION)
    content: str = Field(description=QUESTION_CONTENT_DESCRIPTION)


class _GeneratedSubquestions(BaseModel):
    subquestions: list[_GeneratedSubquestion] = Field(
        default_factory=list,
        description=(
            "Up to ten subquestions for the scope question. Returning fewer "
            "(including zero) is acceptable — the cap exists to prevent "
            "explosions, not as a target."
        ),
    )


class GenerateSubquestionsUpdater(WorkspaceUpdater):
    """Generate subquestions via a single structured LLM call, then
    manually create the corresponding question pages."""

    async def update_workspace(
        self,
        infra: CallInfra,
        context: ContextResult,
    ) -> UpdateResult:
        system_prompt = build_system_prompt(
            CallType.GENERATE_SUBQUESTIONS.value,
            task="Generate subquestions for the scope question",
            include_citations=False,
        )
        user_message = build_user_message(
            context.context_text,
            f"Scope question ID: `{infra.question_id}`",
        )

        result = await structured_call(
            system_prompt=system_prompt,
            user_message=user_message,
            response_model=_GeneratedSubquestions,
            metadata=LLMExchangeMetadata(
                call_id=infra.call.id,
                phase="generate_subquestions",
            ),
            db=infra.db,
            cache=True,
        )

        parsed = result.parsed
        proposals = parsed.subquestions if parsed is not None else []
        log.info(
            "generate_subquestions: model proposed %d subquestion(s)",
            len(proposals),
        )

        created_page_ids: list[str] = []
        moves: list[Move] = []
        move_created_ids: list[list[str]] = []
        for proposal in proposals:
            payload = CreateScoutQuestionPayload(
                headline=proposal.headline,
                content=proposal.content,
                workspace="research",
                supersedes=None,
                change_magnitude=None,
            )
            move_result = await execute_scout_question(payload, infra.call, infra.db)
            page_id = move_result.created_page_id
            if not page_id:
                log.warning(
                    "generate_subquestions: scout-question execute returned no page id "
                    "for proposal headline=%r",
                    proposal.headline[:80],
                )
                continue
            created_page_ids.append(page_id)
            moves.append(Move(move_type=MoveType.CREATE_SCOUT_QUESTION, payload=payload))
            move_created_ids.append([page_id])

        infra.state.created_page_ids.extend(created_page_ids)
        infra.state.moves.extend(moves)
        infra.state.move_created_ids.extend(move_created_ids)
        if created_page_ids:
            infra.state.last_created_id = created_page_ids[-1]

        if moves:
            event = await moves_to_trace_event(moves, move_created_ids, infra.db)
            await infra.trace.record(event)

        return UpdateResult(
            created_page_ids=created_page_ids,
            moves=moves,
            all_loaded_ids=list(context.working_page_ids),
            rounds_completed=1,
        )


class NoopClosingReview(ClosingReviewer):
    """Closing reviewer that does nothing beyond marking the call complete."""

    async def closing_review(
        self,
        infra: CallInfra,
        context: ContextResult,
        creation: UpdateResult,
    ) -> None:
        infra.call.review_json = {}
        summary = f"Generated {len(creation.created_page_ids)} subquestion(s)."
        await mark_call_completed(infra.call, infra.db, summary)


class GenerateSubquestionsCall(CallRunner):
    """Generate subquestions for the scope question via a single structured call."""

    context_builder_cls = EmbeddingContext
    workspace_updater_cls = GenerateSubquestionsUpdater
    closing_reviewer_cls = NoopClosingReview
    call_type = CallType.GENERATE_SUBQUESTIONS

    def _make_context_builder(self) -> ContextBuilder:
        return EmbeddingContext(self.call_type)

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return GenerateSubquestionsUpdater()

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return NoopClosingReview()

    def _resolve_available_moves(self):
        return ()

    def task_description(self) -> str:
        return (
            "Generate subquestions for the scope question.\n\n"
            f"Question ID: `{self.infra.question_id}`"
        )
