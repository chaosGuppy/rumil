"""Critique Artefact call: independently assess the latest artefact on a task.

The critic sees the artefact, the task, and the broader workspace, but NOT
the spec. That asymmetry is intentional — if the critic saw the spec, it
would only verify conformance. By seeing the request directly (via workspace
context) and judging the artefact on its own merits, the critic can surface
the most valuable kind of finding: gaps in the spec itself.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import CritiqueContext, _latest_artefact_for_task
from rumil.calls.stages import (
    CallInfra,
    CallRunner,
    ClosingReviewer,
    ContextBuilder,
    ContextResult,
    UpdateResult,
    WorkspaceUpdater,
)
from rumil.llm import LLMExchangeMetadata, build_system_prompt, structured_call
from rumil.models import (
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.settings import get_settings

log = logging.getLogger(__name__)


class CritiqueOutput(BaseModel):
    grade: int = Field(
        description=(
            "1-10 grade for how well the artefact satisfies the request. "
            "1 = does not satisfy the request at all; "
            "5 = partial — a reviewer would ask for substantial changes; "
            "8 = solid, a few notes but shippable with small edits; "
            "10 = excellent, could not meaningfully improve."
        ),
    )
    overall: str = Field(
        description=(
            "2-5 sentences summarising the artefact's overall fit: what works, "
            "what the biggest issues are, and whether further iteration looks "
            "worthwhile versus the request being too open-ended to converge."
        ),
    )
    issues: list[str] = Field(
        description=(
            "Itemised problems with the artefact. Each entry should be specific "
            "and actionable: what is wrong (or missing), and what the artefact "
            "should do instead. Surface gaps the spec does not cover — that is "
            "where you add the most value."
        ),
    )


class CritiqueWriter(WorkspaceUpdater):
    """Structured-call updater that persists a critique as a hidden JUDGEMENT page.

    Writes one JUDGEMENT page (hidden, with grade+issues stored in ``extra``)
    linked CRITIQUE_OF to the artefact under review.
    """

    async def update_workspace(
        self,
        infra: CallInfra,
        context: ContextResult,
    ) -> UpdateResult:
        artefact = await _latest_artefact_for_task(infra.question_id, infra.db)
        if artefact is None:
            raise RuntimeError(
                f"critique_artefact: no artefact found for task {infra.question_id[:8]}"
            )

        system_prompt = build_system_prompt(CallType.CRITIQUE_ARTEFACT.value)
        result = await structured_call(
            system_prompt=system_prompt,
            user_message=context.context_text,
            response_model=CritiqueOutput,
            metadata=LLMExchangeMetadata(
                call_id=infra.call.id,
                phase="critique_artefact",
            ),
            db=infra.db,
        )
        parsed = result.parsed
        if parsed is None:
            raise RuntimeError(
                f"critique_artefact: structured call for {infra.call.id[:8]} "
                "returned no parsed output"
            )

        issues_md = "\n".join(f"- {issue}" for issue in parsed.issues) or "- (none)"
        content = (
            f"**Grade:** {parsed.grade}/10\n\n"
            f"**Overall:** {parsed.overall}\n\n"
            f"**Issues:**\n{issues_md}"
        )
        headline = f"Critique of {artefact.headline[:80]} (grade {parsed.grade}/10)"

        critique_page = Page(
            page_type=PageType.JUDGEMENT,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content=content,
            headline=headline,
            hidden=True,
            extra={
                "grade": parsed.grade,
                "issues": list(parsed.issues),
            },
            provenance_model=get_settings().model,
            provenance_call_type=infra.call.call_type.value,
            provenance_call_id=infra.call.id,
        )
        await infra.db.save_page(critique_page)

        await infra.db.save_link(
            PageLink(
                from_page_id=critique_page.id,
                to_page_id=artefact.id,
                link_type=LinkType.CRITIQUE_OF,
            )
        )

        log.info(
            "Critique persisted: %s -> artefact %s (grade=%d, issues=%d)",
            critique_page.id[:8],
            artefact.id[:8],
            parsed.grade,
            len(parsed.issues),
        )

        infra.state.created_page_ids.append(critique_page.id)
        infra.state.last_created_id = critique_page.id

        return UpdateResult(
            created_page_ids=[critique_page.id],
            moves=[],
            all_loaded_ids=list(context.working_page_ids),
            rounds_completed=1,
        )


class CritiqueArtefactCall(CallRunner):
    """Independently critique the latest artefact on an artefact-task question.

    Sees the artefact, the task, and the workspace via embedding search.
    Deliberately does NOT see the spec — spec-gaps are the most valuable
    thing the critic can surface for the refiner.
    """

    context_builder_cls = CritiqueContext
    workspace_updater_cls = CritiqueWriter
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.CRITIQUE_ARTEFACT

    def _make_context_builder(self) -> ContextBuilder:
        return CritiqueContext()

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return CritiqueWriter()

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return StandardClosingReview(self.call_type)

    def task_description(self) -> str:
        return (
            "Critique the artefact shown above for how well it satisfies the "
            "artefact-task request, using your knowledge of the broader workspace. "
            "Prioritise issues the artefact's spec might not have covered — "
            "that is where your judgement is most valuable.\n\n"
            f"Artefact-task question ID: `{self.infra.question_id}`"
        )
