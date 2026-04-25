"""Generate Artefact call: write the artefact from the spec alone.

Intentionally domain-neutral — the generator sees only the spec items and the
artefact-task description, not the rumil preamble, not the broader workspace.
Any information the artefact should reflect must be captured in a spec item.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field, ValidationError

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import SpecOnlyContext, active_spec_items_for_task
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

# Artefacts are inherently long-form (multi-page plans, retrospectives,
# designs). The Anthropic SDK refuses non-streaming requests above ~21,333
# max_tokens (its heuristic for "this might take >10 minutes"); we sit just
# below at 20k. That covers an artefact of ~12-15k content tokens plus
# thinking budget, while keeping the request non-streaming. To produce
# longer artefacts we'd need to switch to messages.stream().
_ARTEFACT_MAX_TOKENS = 20_000


class ArtefactOutput(BaseModel):
    headline: str = Field(
        description=(
            "A short, self-contained label for this artefact (10-15 words). "
            "A reader seeing only the headline should know what the artefact is."
        ),
    )
    content: str = Field(
        description=(
            "The artefact itself — the full long-form object the spec describes. "
            "Produce the artefact; do not meta-comment about it."
        ),
    )


class ArtefactWriter(WorkspaceUpdater):
    """Structured-call updater that writes an artefact from the spec.

    Creates one hidden ARTEFACT page. Links the artefact ARTEFACT_OF to the
    artefact-task question and GENERATED_FROM to each spec item that was live
    at generation time (the per-iteration snapshot used later to reconstruct
    past specs even after deletes).

    If an earlier artefact exists for this task, it is superseded.
    """

    async def update_workspace(
        self,
        infra: CallInfra,
        context: ContextResult,
    ) -> UpdateResult:
        system_prompt = build_system_prompt(
            CallType.GENERATE_ARTEFACT.value,
            include_preamble=False,
        )
        try:
            result = await structured_call(
                system_prompt=system_prompt,
                user_message=context.context_text,
                response_model=ArtefactOutput,
                metadata=LLMExchangeMetadata(
                    call_id=infra.call.id,
                    phase="generate_artefact",
                ),
                db=infra.db,
                max_tokens=_ARTEFACT_MAX_TOKENS,
            )
        except ValidationError as exc:
            # messages.parse raises ValidationError when the model's JSON is
            # truncated mid-string (usually a max_tokens hit). Don't crash the
            # call — the refine loop will see this as a tool error via the
            # regenerate_and_critique message and can retry or finalize.
            raise RuntimeError(
                f"generate_artefact: model output was truncated or malformed "
                f"({exc.error_count()} pydantic error(s)). The artefact may "
                "be too long for the current max_tokens; try a smaller request "
                "or split the task."
            ) from exc
        parsed = result.parsed
        if parsed is None:
            raise RuntimeError(
                f"generate_artefact: structured call for {infra.call.id[:8]} "
                "returned no parsed output"
            )

        artefact_page = Page(
            page_type=PageType.ARTEFACT,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content=parsed.content,
            headline=parsed.headline,
            hidden=True,
            provenance_model=get_settings().model,
            provenance_call_type=infra.call.call_type.value,
            provenance_call_id=infra.call.id,
        )
        await infra.db.save_page(artefact_page)

        await infra.db.save_link(
            PageLink(
                from_page_id=artefact_page.id,
                to_page_id=infra.question_id,
                link_type=LinkType.ARTEFACT_OF,
            )
        )

        spec_items = await active_spec_items_for_task(infra.question_id, infra.db)
        for spec in spec_items:
            await infra.db.save_link(
                PageLink(
                    from_page_id=artefact_page.id,
                    to_page_id=spec.id,
                    link_type=LinkType.GENERATED_FROM,
                )
            )

        prior_artefact = await _prior_artefact(infra.question_id, artefact_page.id, infra.db)
        if prior_artefact is not None:
            await infra.db.supersede_page(prior_artefact.id, artefact_page.id)
            log.info(
                "Superseded prior artefact %s with %s",
                prior_artefact.id[:8],
                artefact_page.id[:8],
            )

        log.info(
            "Artefact generated: %s (task=%s, spec_items=%d)",
            artefact_page.id[:8],
            infra.question_id[:8],
            len(spec_items),
        )

        infra.state.created_page_ids.append(artefact_page.id)
        infra.state.last_created_id = artefact_page.id

        return UpdateResult(
            created_page_ids=[artefact_page.id],
            moves=[],
            all_loaded_ids=list(context.working_page_ids),
            rounds_completed=1,
        )


async def _prior_artefact(task_id: str, new_artefact_id: str, db) -> Page | None:
    """Return the most recent active ARTEFACT linked ARTEFACT_OF to the task,
    excluding *new_artefact_id*. Used to supersede the prior version in place."""
    links = await db.get_links_to(task_id)
    artefact_links = [l for l in links if l.link_type == LinkType.ARTEFACT_OF]
    candidate_ids = [l.from_page_id for l in artefact_links if l.from_page_id != new_artefact_id]
    if not candidate_ids:
        return None
    pages_by_id = await db.get_pages_by_ids(candidate_ids)
    active = [p for p in pages_by_id.values() if p.is_active() and p.page_type == PageType.ARTEFACT]
    if not active:
        return None
    return max(active, key=lambda p: p.created_at)


class GenerateArtefactCall(CallRunner):
    """Produce the artefact from the current spec on an artefact-task question.

    The generator sees only the spec (via SpecOnlyContext) and writes the
    artefact in one structured call. No rumil preamble, no broader workspace.
    """

    context_builder_cls = SpecOnlyContext
    workspace_updater_cls = ArtefactWriter
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.GENERATE_ARTEFACT

    def _make_context_builder(self) -> ContextBuilder:
        return SpecOnlyContext()

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return ArtefactWriter()

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return StandardClosingReview(self.call_type)

    def task_description(self) -> str:
        return (
            "Produce the artefact described by the spec in your user message. "
            f"Artefact-task question ID: `{self.infra.question_id}`"
        )
