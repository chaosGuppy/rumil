"""Critique Artefact (Request-Only) call: second-opinion critic with no workspace context.

Pairs with critique_artefact (workspace-aware) so each artefact gets two
critiques per regeneration: one informed by the broader workspace, one
unbiased by it. The request-only critic catches issues like "the artefact
doesn't actually do what the request asked for" that a workspace-aware
critic might rationalise away because of context the workspace happens
to carry. The refiner reads both critiques and triangulates.
"""

import logging

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import RequestOnlyCritiqueContext
from rumil.calls.critique_artefact import CritiqueOutput
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


class RequestOnlyCritiqueWriter(WorkspaceUpdater):
    """Structured-call updater that persists a request-only critique.

    Same JUDGEMENT shape as CritiqueWriter (grade + issues in extra,
    CRITIQUE_OF link to the artefact). Differs only in the prompt and
    context — and in the resulting headline, so the refiner can tell
    the two critiques apart at a glance.
    """

    async def update_workspace(
        self,
        infra: CallInfra,
        context: ContextResult,
    ) -> UpdateResult:
        artefact = await infra.db.latest_artefact_for_task(infra.question_id)
        if artefact is None:
            raise RuntimeError(
                f"critique_artefact_request_only: no artefact found for task "
                f"{infra.question_id[:8]}"
            )

        system_prompt = build_system_prompt(
            CallType.CRITIQUE_ARTEFACT_REQUEST_ONLY.value,
            include_preamble=False,
        )
        result = await structured_call(
            system_prompt=system_prompt,
            user_message=context.context_text,
            response_model=CritiqueOutput,
            metadata=LLMExchangeMetadata(
                call_id=infra.call.id,
                phase="critique_artefact_request_only",
            ),
            db=infra.db,
        )
        parsed = result.parsed
        if parsed is None:
            raise RuntimeError(
                f"critique_artefact_request_only: structured call for "
                f"{infra.call.id[:8]} returned no parsed output"
            )

        issues_md = "\n".join(f"- {issue}" for issue in parsed.issues) or "- (none)"
        content = (
            f"**Grade (request-only):** {parsed.grade}/10\n\n"
            f"**Overall:** {parsed.overall}\n\n"
            f"**Issues:**\n{issues_md}"
        )
        headline = f"Request-only critique of {artefact.headline[:70]} (grade {parsed.grade}/10)"

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
                "critique_kind": "request_only",
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
            "Request-only critique persisted: %s -> artefact %s (grade=%d, issues=%d)",
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


class RequestOnlyCritiqueArtefactCall(CallRunner):
    """Critique the latest artefact using only the task and the artefact.

    No workspace embedding sweep, no preamble — a fresh outside reader.
    Run alongside CritiqueArtefactCall on each regeneration so the refiner
    sees both context-aware and context-blind angles.
    """

    context_builder_cls = RequestOnlyCritiqueContext
    workspace_updater_cls = RequestOnlyCritiqueWriter
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.CRITIQUE_ARTEFACT_REQUEST_ONLY

    def _make_context_builder(self) -> ContextBuilder:
        return RequestOnlyCritiqueContext()

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return RequestOnlyCritiqueWriter()

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return StandardClosingReview(self.call_type)

    def task_description(self) -> str:
        return (
            "Critique the artefact shown above purely on whether it does what "
            "the request asks for. You have only the task and the artefact — "
            "no broader context. Stay close to the request text.\n\n"
            f"Artefact-task question ID: `{self.infra.question_id}`"
        )
