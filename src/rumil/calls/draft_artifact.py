"""Draft Artifact call: render an external-facing document from a question's View.

This is rumil's first output-side generation primitive. It takes a question,
reads the question's View (the distilled research state), and produces a
shape-parameterized document (strategy_brief, scenario_forecast, market_research)
as an ARTIFACT page.

By default the call is a single-pass render — no agent loop, no tools, no
refinement. The `RefineArtifactOrchestrator` composes this call with
`AdversarialReviewCall` to produce a draft -> review -> refine loop; when
that orchestrator invokes this call on a non-first iteration it passes a
`RefineContext` that swaps in a refine-specific prompt and includes the
prior draft + adversarial dissents in the user message.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.common import resolve_page_refs
from rumil.calls.stages import (
    CallInfra,
    CallRunner,
    ClosingReviewer,
    ContextBuilder,
    ContextResult,
    UpdateResult,
    WorkspaceUpdater,
)
from rumil.context import format_page
from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, _load_file, structured_call
from rumil.models import (
    Call,
    CallType,
    LinkType,
    Page,
    PageDetail,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.settings import get_settings
from rumil.tracing.trace_events import ContextBuiltEvent
from rumil.views import build_view

log = logging.getLogger(__name__)


DRAFT_ARTIFACT_PROMPT_FILE = "draft_artifact.md"
REFINE_ARTIFACT_PROMPT_FILE = "refine_artifact.md"

ArtifactShape = Literal["strategy_brief", "scenario_forecast", "market_research"]

SUPPORTED_SHAPES: tuple[ArtifactShape, ...] = (
    "strategy_brief",
    "scenario_forecast",
    "market_research",
)

DEFAULT_SHAPE: ArtifactShape = "strategy_brief"
DEFAULT_IMPORTANCE_THRESHOLD = 2
DEFAULT_TOP_N_ITEMS = 15


class RefineContext(BaseModel):
    """Inputs that turn a DraftArtifact call into a refinement pass.

    Passed to ``DraftArtifactCall`` / ``DraftArtifactUpdater`` when the caller
    (typically ``RefineArtifactOrchestrator``) wants this draft to build on a
    previous iteration + address adversarial dissents. When present, the
    updater swaps to the refine prompt and appends the prior draft, dissents,
    and concurrences to the user message.
    """

    prior_title: str = Field(description="Title of the prior draft.")
    prior_body_markdown: str = Field(description="Body markdown of the prior draft.")
    dissents: list[str] = Field(
        default_factory=list,
        description="Surviving objections from the adversarial review to address.",
    )
    concurrences: list[str] = Field(
        default_factory=list,
        description="Strengths from the prior iteration worth preserving.",
    )
    iteration: int = Field(
        description="Which refinement iteration this is (1-indexed; 1 = first refine).",
    )


class DraftArtifactResult(BaseModel):
    """Structured output of the draft-artifact LLM call."""

    title: str = Field(
        description="Short, specific title for the artifact (under 15 words).",
    )
    body_markdown: str = Field(
        description=(
            "The full document body in markdown. Do NOT include the title "
            "inside body_markdown — it is stored separately on the page."
        ),
    )
    key_claims: list[str] = Field(
        default_factory=list,
        description=(
            "Short page IDs (8-char prefixes) of the View items that anchor "
            "the most load-bearing claims in the document. 3-10 items."
        ),
    )
    open_questions: list[str] = Field(
        default_factory=list,
        description=(
            "2-5 short strings naming specific things that would most reduce "
            "uncertainty if investigated."
        ),
    )


class ArtifactContext(ContextBuilder):
    """Build context from the target question's View.

    Loads the question headline + abstract and the top-N view items by
    importance at the full-content level. The full research tree is NOT
    included — the View is the contract.
    """

    def __init__(
        self,
        *,
        importance_threshold: int = DEFAULT_IMPORTANCE_THRESHOLD,
        top_n_items: int = DEFAULT_TOP_N_ITEMS,
    ) -> None:
        self._importance_threshold = importance_threshold
        self._top_n_items = top_n_items

    async def build_context(self, infra: CallInfra) -> ContextResult:
        question = await infra.db.get_page(infra.question_id)
        if question is None:
            raise ValueError(f"DraftArtifact: question page {infra.question_id!r} not found.")

        view = await build_view(
            infra.db,
            infra.question_id,
            importance_threshold=self._importance_threshold,
        )

        parts: list[str] = []
        parts.append(f"# Question\n\n## {question.headline}\n")
        parts.append(f"**Question ID:** `{question.id}`\n")
        if question.abstract:
            parts.append(f"**Abstract:** {question.abstract}\n")
        if question.content and question.content != question.headline:
            parts.append(f"**Content:**\n\n{question.content}\n")

        all_items = [item for section in view.sections for item in section.items]
        all_items.sort(key=lambda item: item.sort_key)
        top_items = all_items[: self._top_n_items]

        working_page_ids: list[str] = [question.id]
        item_page_ids: list[str] = []

        if not top_items:
            parts.append(
                "\n## View\n\n"
                "_No distilled view is available for this question yet. "
                "The View has no items at or above the importance threshold._\n"
            )
        else:
            parts.append(f"\n## View Items (top {len(top_items)} by importance)\n")
            for item in top_items:
                page = item.page
                item_page_ids.append(page.id)
                working_page_ids.append(page.id)
                scores: list[str] = []
                if page.credence is not None:
                    scores.append(f"C{page.credence}")
                if page.robustness is not None:
                    scores.append(f"R{page.robustness}")
                if page.importance is not None:
                    scores.append(f"I{page.importance}")
                score_str = "/".join(scores) if scores else "unscored"

                rendered = await format_page(
                    page,
                    PageDetail.CONTENT,
                    linked_detail=None,
                    db=infra.db,
                    track=True,
                    track_tags={"source": "draft_artifact_view_item"},
                )
                parts.append(
                    f"\n### [{page.page_type.value.upper()} {score_str}] "
                    f"`{page.id[:8]}` ({item.section}) — {page.headline}\n\n"
                    f"{rendered}\n"
                )

        context_text = "\n".join(parts)

        refs = await resolve_page_refs(working_page_ids, infra.db)
        await infra.trace.record(
            ContextBuiltEvent(
                working_context_page_ids=refs,
                preloaded_page_ids=[],
                source_page_id=None,
                scout_mode=None,
            )
        )

        return ContextResult(
            context_text=context_text,
            working_page_ids=working_page_ids,
            preloaded_ids=[],
        )


def _format_system_prompt(template: str, shape: str) -> str:
    """Expand the `{shape}` placeholder without touching other braces."""
    return template.replace("{shape}", shape)


async def _persist_artifact(
    result: DraftArtifactResult,
    shape: str,
    question_id: str,
    item_page_ids_by_short: dict[str, str],
    call: Call,
    db: DB,
) -> str:
    """Write the artifact as an ARTIFACT page, link it, and return the page id."""
    page = Page(
        page_type=PageType.ARTIFACT,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        content=result.body_markdown,
        headline=result.title[:200],
        abstract="",
        provenance_model=get_settings().model,
        provenance_call_type=call.call_type.value,
        provenance_call_id=call.id,
        extra={
            "shape": shape,
            "open_questions": list(result.open_questions),
            "key_claims_short_ids": list(result.key_claims),
        },
    )
    await db.save_page(page)

    await db.save_link(
        PageLink(
            from_page_id=page.id,
            to_page_id=question_id,
            link_type=LinkType.RELATED,
            reasoning="Artifact drafted from view",
        )
    )

    linked_short_ids: list[str] = []
    for short in result.key_claims:
        full_id = item_page_ids_by_short.get(short[:8])
        if full_id is None:
            full_id = await db.resolve_page_id(short)
            if full_id and db.project_id:
                resolved_page = await db.get_page(full_id)
                if resolved_page is None or resolved_page.project_id != db.project_id:
                    log.info(
                        "DraftArtifact: key_claim %r resolved to page %s outside "
                        "project %s; skipping.",
                        short,
                        full_id[:8],
                        db.project_id,
                    )
                    full_id = None
        if not full_id:
            log.info(
                "DraftArtifact: key_claim %r did not resolve to a page; skipping.",
                short,
            )
            continue
        await db.save_link(
            PageLink(
                from_page_id=page.id,
                to_page_id=full_id,
                link_type=LinkType.CITES,
                reasoning="Artifact cites this view item",
            )
        )
        linked_short_ids.append(full_id[:8])

    log.info(
        "DraftArtifact: created artifact %s (%s) for question %s — %d cites",
        page.id[:8],
        shape,
        question_id[:8],
        len(linked_short_ids),
    )
    return page.id


def _format_refine_user_message(
    context_text: str,
    shape: str,
    refine: RefineContext,
) -> str:
    """Append prior draft + dissents + concurrences to the base context block."""
    dissents_block = "\n".join(f"- {d}" for d in refine.dissents) if refine.dissents else "(none)"
    concurrences_block = (
        "\n".join(f"- {c}" for c in refine.concurrences) if refine.concurrences else "(none)"
    )
    return (
        f"{context_text}\n\n---\n\n"
        f"## Prior draft (iteration {refine.iteration - 1})\n\n"
        f"### Title\n{refine.prior_title}\n\n"
        f"### Body\n\n{refine.prior_body_markdown}\n\n---\n\n"
        f"## Adversarial dissents to address (iteration {refine.iteration})\n\n"
        f"{dissents_block}\n\n---\n\n"
        f"## Concurrences to preserve\n\n"
        f"{concurrences_block}\n\n---\n\n"
        f"Produce the revised artifact now. Shape: `{shape}`. "
        "Mark substantive changes from the prior draft with inline "
        "`🔧 changed in revision:` notes."
    )


class DraftArtifactUpdater(WorkspaceUpdater):
    """Render the artifact via a single structured_call against the View context.

    When ``refine`` is provided, swaps to the refine prompt and includes the
    prior draft + adversarial dissents in the user message.
    """

    def __init__(
        self,
        *,
        shape: ArtifactShape = DEFAULT_SHAPE,
        refine: RefineContext | None = None,
    ) -> None:
        if shape not in SUPPORTED_SHAPES:
            raise ValueError(
                f"DraftArtifact: unsupported shape {shape!r}. "
                f"Supported: {', '.join(SUPPORTED_SHAPES)}"
            )
        self._shape = shape
        self._refine = refine

    async def update_workspace(
        self,
        infra: CallInfra,
        context: ContextResult,
    ) -> UpdateResult:
        if self._refine is not None:
            template = _load_file(REFINE_ARTIFACT_PROMPT_FILE)
            user_message = _format_refine_user_message(
                context.context_text, self._shape, self._refine
            )
        else:
            template = _load_file(DRAFT_ARTIFACT_PROMPT_FILE)
            user_message = (
                f"{context.context_text}\n\n---\n\nDraft the artifact now. Shape: `{self._shape}`."
            )
        system_prompt = _format_system_prompt(template, self._shape)

        item_page_ids_by_short: dict[str, str] = {pid[:8]: pid for pid in context.working_page_ids}

        meta = LLMExchangeMetadata(
            call_id=infra.call.id,
            phase="update_workspace",
            user_message=user_message,
        )
        call_result = await structured_call(
            system_prompt=system_prompt,
            user_message=user_message,
            response_model=DraftArtifactResult,
            metadata=meta,
            db=infra.db,
        )
        if call_result.parsed is None:
            raise ValueError("DraftArtifact: LLM returned no parseable artifact.")
        parsed: DraftArtifactResult = call_result.parsed

        artifact_id = await _persist_artifact(
            parsed,
            self._shape,
            infra.question_id,
            item_page_ids_by_short,
            infra.call,
            infra.db,
        )
        infra.state.created_page_ids.append(artifact_id)

        return UpdateResult(
            created_page_ids=[artifact_id],
            moves=[],
            all_loaded_ids=list(context.working_page_ids),
            rounds_completed=1,
        )


class DraftArtifactCall(CallRunner):
    """Render a shape-parameterized external-facing artifact from a View.

    This is a one-shot render — no agent loop, no tools, no refinement.
    The artifact lands as an ARTIFACT page linked RELATED to the source
    question and CITES to the view items it anchors on.
    """

    context_builder_cls = ArtifactContext
    workspace_updater_cls = DraftArtifactUpdater
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.DRAFT_ARTIFACT

    def __init__(
        self,
        question_id: str,
        call: Call,
        db: DB,
        *,
        shape: ArtifactShape = DEFAULT_SHAPE,
        importance_threshold: int = DEFAULT_IMPORTANCE_THRESHOLD,
        top_n_items: int = DEFAULT_TOP_N_ITEMS,
        refine: RefineContext | None = None,
        broadcaster=None,
        up_to_stage=None,
    ) -> None:
        if shape not in SUPPORTED_SHAPES:
            raise ValueError(
                f"DraftArtifact: unsupported shape {shape!r}. "
                f"Supported: {', '.join(SUPPORTED_SHAPES)}"
            )
        self._shape: ArtifactShape = shape
        self._importance_threshold = importance_threshold
        self._top_n_items = top_n_items
        self._refine = refine
        super().__init__(
            question_id,
            call,
            db,
            broadcaster=broadcaster,
            up_to_stage=up_to_stage,
            max_rounds=1,
            fruit_threshold=0,
        )

    def _make_context_builder(self) -> ContextBuilder:
        return ArtifactContext(
            importance_threshold=self._importance_threshold,
            top_n_items=self._top_n_items,
        )

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return DraftArtifactUpdater(shape=self._shape, refine=self._refine)

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return StandardClosingReview(self.call_type)

    def task_description(self) -> str:
        return (
            f"Draft an external-facing artifact of shape `{self._shape}` "
            f"from the View of question `{self.infra.question_id}`.\n\n"
            "Use the top view items by importance as the evidence base. "
            "Anchor load-bearing claims in specific view item IDs. "
            "Preserve epistemic uncertainty — do not flatten credence 5 items "
            "into certain statements."
        )
