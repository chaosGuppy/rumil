"""LinkSubquestionsCall: explore workspace and link relevant subquestions to a scope question."""

import logging

from pydantic import BaseModel, Field

from rumil.calls.common import mark_call_completed, prepare_tools, run_agent_loop
from rumil.calls.stages import (
    CallInfra,
    CallRunner,
    ClosingReviewer,
    ContextBuilder,
    ContextResult,
    UpdateResult,
    WorkspaceUpdater,
)
from rumil.llm import LLMExchangeMetadata, structured_call
from rumil.models import CallType, LinkType
from rumil.moves.base import link_pages
from rumil.scope_subquestion_linker.prompt import build_linker_prompt
from rumil.scope_subquestion_linker.seed_selection import select_seed_questions
from rumil.scope_subquestion_linker.subgraph import render_question_subgraph
from rumil.scope_subquestion_linker.validation import validate_proposals
from rumil.scope_subquestion_linker.tool import (
    LinkerResult,
    SubmitHolder,
    make_render_subgraph_tool,
    make_submit_tool,
)
from rumil.settings import get_settings
from rumil.tracing.trace_events import (
    ContextBuiltEvent,
    LinkSubquestionsCompleteEvent,
    ProposedSubquestion,
    ReviewCompleteEvent,
)

log = logging.getLogger(__name__)


class LinkerContextBuilder(ContextBuilder):
    """Select relevant seed questions, render their subgraphs, and build context."""

    async def build_context(self, infra: CallInfra) -> ContextResult:
        settings = get_settings()
        scope = await infra.db.get_page(infra.question_id)
        if scope is None:
            raise ValueError(f"Scope question {infra.question_id} not found")

        seeds = await select_seed_questions(
            scope, infra.db, limit=settings.scope_subquestion_linker_seed_limit
        )
        log.info("LinkerContextBuilder: selected %d seed question(s)", len(seeds))

        seed_blocks: list[str] = []
        for seed in seeds:
            sub = await render_question_subgraph(
                seed.id,
                infra.db,
                max_pages=settings.scope_subquestion_linker_subgraph_max_pages,
                exclude_ids={scope.id},
            )
            if sub:
                seed_blocks.append(sub)
        seed_block = "\n\n".join(seed_blocks)

        current_children = await infra.db.get_child_questions(infra.question_id)
        if current_children:
            children_block = "\n".join(
                f"- `{c.id[:8]}` -- {c.headline}" for c in current_children
            )
        else:
            children_block = "(none)"

        context_text = (
            "## Scope Question\n\n"
            f"`{scope.id[:8]}` -- {scope.headline}\n\n"
            f"{scope.content or scope.abstract}\n\n"
            "## Currently-Linked Subquestions\n\n"
            f"{children_block}\n\n"
            "## Seed Subgraphs (most relevant top-level questions)\n\n"
            f"{seed_block or '(none)'}\n"
        )

        await infra.trace.record(ContextBuiltEvent())

        return ContextResult(
            context_text=context_text,
            working_page_ids=[infra.question_id],
        )


class LinkerWorkspaceUpdater(WorkspaceUpdater):
    """Run the linker agent loop and materialize proposed links."""

    async def update_workspace(
        self,
        infra: CallInfra,
        context: ContextResult,
    ) -> UpdateResult:
        settings = get_settings()
        max_rounds = settings.scope_subquestion_linker_max_rounds

        system_prompt = build_linker_prompt(max_rounds)
        user_message = (
            f"Find subquestions to link to scope `{infra.question_id[:8]}`.\n\n"
            f"{context.context_text}"
        )

        holder = SubmitHolder()
        tools = [
            make_render_subgraph_tool(infra.db, infra.trace),
            make_submit_tool(holder),
        ]

        agent_result = await run_agent_loop(
            system_prompt,
            user_message,
            tools,
            call_id=infra.call.id,
            db=infra.db,
            state=infra.state,
            max_rounds=max_rounds,
            cache=True,
        )

        if holder.result is None:
            log.warning("Linker agent did not call submit_linked_subquestions")
            holder.result = LinkerResult(question_ids=[])

        current_children = await infra.db.get_child_questions(infra.question_id)
        current_children_ids = {c.id for c in current_children}

        proposed_pages = await validate_proposals(
            holder.result, infra.db, infra.question_id, current_children_ids
        )
        proposed_ids = [p.id for p in proposed_pages]

        created = 0
        for child_id in proposed_ids:
            try:
                await link_pages(
                    infra.question_id,
                    child_id,
                    "Auto-linked by subquestion linker",
                    infra.db,
                    LinkType.CHILD_QUESTION,
                )
                created += 1
            except Exception as e:
                log.warning(
                    "Failed to link proposed subquestion %s -> %s: %s",
                    infra.question_id[:8],
                    child_id[:8],
                    e,
                )

        log.info(
            "Linker: created %d/%d CHILD_QUESTION links for question=%s",
            created,
            len(proposed_ids),
            infra.question_id[:8],
        )

        proposed = [
            ProposedSubquestion(id=p.id, headline=p.headline) for p in proposed_pages
        ]
        await infra.trace.record_strict(
            LinkSubquestionsCompleteEvent(proposed=proposed)
        )

        return UpdateResult(
            created_page_ids=[],
            moves=infra.state.moves,
            all_loaded_ids=[],
            messages=agent_result.messages,
        )


class LinkerFruitAssessment(BaseModel):
    remaining_linkable_questions: int = Field(
        description=(
            "Your best estimate of how many additional questions in the workspace "
            "would clear the relevance bar for linking to the scope, beyond the ones "
            "you just linked. 0 means you believe you found all strong candidates."
        )
    )
    brief_reasoning: str = Field(
        description="One or two sentences explaining your estimate."
    )


_FRUIT_ASSESSMENT_PROMPT = (
    "You have just finished exploring the workspace and linking subquestions. "
    "Now estimate how many additional questions in the workspace would clear "
    "the relevance bar for linking to the scope question, beyond the ones you "
    "just selected. Consider: how thoroughly did you explore? Were there "
    "promising branches you did not fully investigate? How large and diverse "
    "is the workspace relative to what you covered?\n\n"
    "Do not call any tools -- they will have no effect here."
)


class LinkerClosingReviewer(ClosingReviewer):
    """Assess remaining linkable questions using the agent's conversation context."""

    async def closing_review(
        self,
        infra: CallInfra,
        context: ContextResult,
        creation: UpdateResult,
    ) -> None:
        if not creation.messages:
            infra.call.review_json = {"remaining_linkable_questions": 0}
            await mark_call_completed(
                infra.call, infra.db, "Linker complete (no messages)."
            )
            return

        system_prompt = build_linker_prompt(
            get_settings().scope_subquestion_linker_max_rounds
        )
        tools = [
            make_render_subgraph_tool(infra.db, infra.trace),
            make_submit_tool(SubmitHolder()),
        ]
        tool_defs, _ = prepare_tools(tools)

        assessment_messages = [
            *creation.messages,
            {"role": "user", "content": _FRUIT_ASSESSMENT_PROMPT},
        ]
        meta = LLMExchangeMetadata(
            call_id=infra.call.id,
            phase="closing_review",
            user_message=_FRUIT_ASSESSMENT_PROMPT,
        )
        result = await structured_call(
            system_prompt=system_prompt,
            response_model=LinkerFruitAssessment,
            messages=assessment_messages,
            tools=tool_defs,
            metadata=meta,
            db=infra.db,
            cache=True,
        )

        remaining = 0
        reasoning = ""
        if result.parsed:
            remaining = result.parsed.remaining_linkable_questions
            reasoning = result.parsed.brief_reasoning
            log.info(
                "Linker fruit assessment: remaining=%d, reasoning=%s",
                remaining,
                reasoning,
            )

        infra.call.review_json = {
            "remaining_linkable_questions": remaining,
            "brief_reasoning": reasoning,
        }
        await infra.trace.record_strict(ReviewCompleteEvent(remaining_fruit=remaining))
        await mark_call_completed(
            infra.call,
            infra.db,
            f"Linker complete. Estimated {remaining} remaining linkable question(s).",
        )


class LinkSubquestionsCall(CallRunner):
    """Explore workspace and link relevant subquestions to a scope question."""

    context_builder_cls = LinkerContextBuilder
    workspace_updater_cls = LinkerWorkspaceUpdater
    closing_reviewer_cls = LinkerClosingReviewer
    call_type = CallType.LINK_SUBQUESTIONS

    def _make_context_builder(self) -> ContextBuilder:
        return LinkerContextBuilder()

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return LinkerWorkspaceUpdater()

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return LinkerClosingReviewer()

    def task_description(self) -> str:
        return (
            "Find and link subquestions whose answers would strongly and directly "
            f"influence the scope question.\n\nScope question ID: `{self.infra.question_id}`"
        )
