"""Update View call: incrementally update an existing View page."""

import logging
import re
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import pydantic
from pydantic import BaseModel, Field, model_validator

from rumil.calls.closing_reviewers import ViewClosingReview
from rumil.calls.stages import (
    CallInfra,
    CallRunner,
    ClosingReviewer,
    ContextBuilder,
    ContextResult,
    UpdateResult,
    WorkspaceUpdater,
)
from rumil.constants import DEFAULT_VIEW_SECTIONS
from rumil.context import (
    build_embedding_based_context,
    render_child_investigation_results,
    render_claim_investigation_findings,
)
from rumil.database import DB
from rumil.embeddings import embed_and_store_page
from rumil.llm import LLMExchangeMetadata, structured_call
from rumil.models import (
    Call,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.moves.base import extract_and_link_citations
from rumil.orchestrators.common import _split_into_batches
from rumil.settings import get_settings
from rumil.tracing.trace_events import (
    PhaseSkippedEvent,
    UpdateViewPhaseCompletedEvent,
    ViewCreatedEvent,
)

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent.parent / "prompts"

PHASE_MARKER_RE = re.compile(r"<!--\s*PHASE:(\w+)\b[^>]*-->")

SCORE_BATCH_SIZE = 10
TRIAGE_BATCH_SIZE = 40
DEEP_REVIEW_BATCH_SIZE = 4


class UnscoredItemScore(BaseModel):
    item_id: str = Field(description="Short ID (first 8 chars) of the VIEW_ITEM page")
    importance: int = Field(description="1-5 importance score", ge=1, le=5)
    section: str = Field(description="Section name for this item")
    robustness: int | None = Field(
        default=None, description="1-5 robustness override (omit to keep current)"
    )
    robustness_reasoning: str | None = Field(
        default=None,
        description=(
            "Where uncertainty in this item stems from and how reducible it is. "
            "Required whenever robustness is set."
        ),
    )

    @model_validator(mode="after")
    def _robustness_requires_reasoning(self) -> "UnscoredItemScore":
        if self.robustness is not None and not (self.robustness_reasoning or "").strip():
            raise ValueError("robustness_reasoning is required when robustness is set")
        return self


class TriageFlag(BaseModel):
    item_id: str = Field(description="Short ID (first 8 chars) of the VIEW_ITEM page")
    flag: Literal["ok", "review"] = Field(description="Whether this item needs deep review")


class ItemReview(BaseModel):
    item_id: str = Field(description="Short ID (first 8 chars) of the VIEW_ITEM page")
    action: Literal["keep", "adjust", "supersede"] = Field(description="What to do with this item")
    new_importance: int | None = Field(default=None, ge=1, le=5)
    new_section: str | None = None
    new_robustness: int | None = Field(default=None, ge=1, le=5)
    new_robustness_reasoning: str | None = Field(
        default=None,
        description=(
            "Where uncertainty in this item stems from and how reducible it "
            "is. Required whenever new_robustness is set."
        ),
    )
    new_headline: str | None = None
    new_content: str | None = None
    reasoning: str = ""

    @model_validator(mode="after")
    def _new_robustness_requires_reasoning(self) -> "ItemReview":
        if self.new_robustness is not None and not (self.new_robustness_reasoning or "").strip():
            raise ValueError("new_robustness_reasoning is required when new_robustness is set")
        return self


class ProposedItem(BaseModel):
    headline: str = Field(description="Clear, specific headline for the new item")
    content: str = Field(description="Content with epistemic gloss")
    robustness: int = Field(ge=1, le=5)
    robustness_reasoning: str = Field(
        description=("Where uncertainty in this item stems from and how reducible it is."),
    )
    importance: int = Field(ge=1, le=5)
    section: str = Field(description="Section name")
    reasoning: str = ""


class DeepReviewBatchResponse(BaseModel):
    item_reviews: list[ItemReview] = Field(description="One review per item in the batch")
    proposed_items: list[ProposedItem] = Field(
        default_factory=list,
        description="New items to add to the View (zero or more)",
    )


class DemotionChoice(BaseModel):
    item_id: str = Field(description="Short ID (first 8 chars) of the VIEW_ITEM page")
    new_importance: int = Field(ge=1, le=5)
    reasoning: str = ""


class PruneDecision(BaseModel):
    item_id: str = Field(description="Short ID (first 8 chars) of the VIEW_ITEM page")
    action: Literal["keep", "remove"] = Field(description="Whether to keep or remove this item")
    reasoning: str = ""


def _is_explicit_duplicate(page: Page) -> bool:
    """True if the item's content is explicitly marked as a duplicate."""
    return "[Duplicate of" in (page.content or "")


def _prune_candidates(
    items: Sequence[tuple[Page, PageLink]],
) -> list[tuple[Page, PageLink]]:
    """Items eligible for the prune phase: low-importance plus explicit duplicates."""
    return [
        (p, l)
        for p, l in items
        if l.importance is not None and (l.importance <= 2 or _is_explicit_duplicate(p))
    ]


def _parse_prompt_sections(text: str) -> dict[str, str]:
    """Split prompt text on <!-- PHASE:xxx --> markers into {name: content}."""
    parts = PHASE_MARKER_RE.split(text)
    sections: dict[str, str] = {}
    for i in range(1, len(parts), 2):
        sections[parts[i]] = parts[i + 1].strip()
    return sections


def _load_prompt_sections() -> dict[str, str]:
    path = PROMPTS_DIR / "update_view.md"
    return _parse_prompt_sections(path.read_text(encoding="utf-8"))


def _build_update_view_system_prompt(context_section: str) -> str:
    """Build system prompt: preamble + View context framing + grounding.

    No citations section: update_view only modifies epistemic scores and
    section/importance on existing items — it cannot create content-bearing
    pages, so inline-citation rules have nothing to attach to.
    """
    parts: list[str] = []
    for name in ("preamble.md", "grounding.md"):
        p = PROMPTS_DIR / name
        if p.exists():
            parts.append(p.read_text(encoding="utf-8"))
    return (
        parts[0] + "\n\n---\n\n" + context_section + "\n\n---\n\n" + "\n\n---\n\n".join(parts[1:])
    )


def _render_item_compact(page: Page, link: PageLink) -> str:
    """One-line compact rendering for triage."""
    imp = f"I{link.importance}" if link.importance is not None else "I?"
    snippet = (page.content or "")[:120].replace("\n", " ")
    return (
        f"- [{page.id[:8]}] R{page.robustness} {imp} "
        f"sec={link.section or '?'} — {page.headline}\n"
        f"  {snippet}"
    )


def _render_item_full(
    page: Page,
    link: PageLink,
    cited_pages: dict[str, Page] | None = None,
    item_links: Sequence[PageLink] | None = None,
    related_considerations: Sequence[Page] = (),
) -> str:
    """Full rendering with cited pages for deep review."""
    imp = f"I{link.importance}" if link.importance is not None else "I?"
    parts = [
        f"### [{page.id[:8]}] R{page.robustness} {imp} sec={link.section or '?'} — {page.headline}",
        "",
        page.content or "(no content)",
    ]

    cited_ids: set[str] = set()
    if cited_pages and item_links:
        cite_ids = {
            l.to_page_id for l in item_links if l.link_type in (LinkType.CITES, LinkType.DEPENDS_ON)
        }
        cited = [cited_pages[cid] for cid in cite_ids if cid in cited_pages]
        cited_ids = {cp.id for cp in cited}
        if cited:
            parts.append("")
            parts.append("**Cited evidence:**")
            for cp in cited:
                score_parts = []
                if cp.credence is not None:
                    score_parts.append(f"C{cp.credence}")
                if cp.robustness is not None:
                    score_parts.append(f"R{cp.robustness}")
                score_str = f" {'/'.join(score_parts)}" if score_parts else ""
                parts.append(f"- `{cp.id[:8]}` [{cp.page_type.value}]{score_str} — {cp.headline}")
                if cp.abstract:
                    parts.append(f"  {cp.abstract[:200]}")

    uncited = [c for c in related_considerations if c.id not in cited_ids and c.id != page.id]
    if uncited:
        parts.append("")
        parts.append("**Related considerations on the parent question (not cited by this item):**")
        for cp in uncited:
            score_parts = []
            if cp.credence is not None:
                score_parts.append(f"C{cp.credence}")
            if cp.robustness is not None:
                score_parts.append(f"R{cp.robustness}")
            score_str = f" {'/'.join(score_parts)}" if score_parts else ""
            parts.append(f"- `{cp.id[:8]}`{score_str} — {cp.headline}")
            if cp.abstract:
                parts.append(f"  {cp.abstract[:200]}")

    return "\n".join(parts)


class UpdateViewContext(ContextBuilder):
    """Context for View update: embedding-based with raised similarity floors.

    The new view page doesn't exist yet at build_context time (it's created
    at update_workspace time so ``--up-to-stage build_context`` stays
    side-effect-free). We read items from the *old* view — the pending copy
    preserves each item's target page, importance, section, and position, so
    the context is byte-identical to what the new view would expose.
    """

    def __init__(self) -> None:
        pass

    async def build_context(self, infra: CallInfra) -> ContextResult:
        question = await infra.db.get_page(infra.question_id)
        query = question.headline if question else infra.question_id

        old_view = await infra.db.get_view_for_question(infra.question_id)
        if not old_view:
            raise RuntimeError(
                f"UpdateViewContext requires an existing View for question "
                f"{infra.question_id[:8]}, but none was found."
            )
        last_view_created_at = old_view.created_at

        child_section, child_page_ids = await render_child_investigation_results(
            infra.db,
            infra.question_id,
            last_view_created_at,
        )

        claim_section, claim_page_ids = await render_claim_investigation_findings(
            infra.db,
            infra.question_id,
            last_view_created_at,
        )

        items = await infra.db.get_view_items(old_view.id)
        item_ids = [page.id for page, _ in items]
        links_by_item = await infra.db.get_links_from_many(item_ids) if item_ids else {}
        cited_ids: set[str] = set()
        for item_links in links_by_item.values():
            for link in item_links:
                if link.link_type in (LinkType.CITES, LinkType.DEPENDS_ON):
                    cited_ids.add(link.to_page_id)

        exclude_ids = cited_ids | set(item_ids) | set(child_page_ids) | set(claim_page_ids)

        result = await build_embedding_based_context(
            query,
            infra.db,
            scope_question_id=infra.question_id,
            require_judgement_for_questions=True,
            full_page_similarity_floor=0.6,
            abstract_page_similarity_floor=0.5,
            summary_page_similarity_floor=0.4,
            exclude_page_ids=exclude_ids,
        )

        context_text = result.context_text
        if claim_section:
            context_text = claim_section + "\n\n" + context_text
        if child_section:
            context_text = child_section + "\n\n" + context_text

        preloaded_ids = list(infra.call.context_page_ids or [])
        return ContextResult(
            context_text=context_text,
            working_page_ids=result.page_ids + child_page_ids + claim_page_ids,
            preloaded_ids=preloaded_ids,
        )


class UpdateViewWorkspaceUpdater(WorkspaceUpdater):
    """Multi-phase workspace updater for incremental View updates.

    Materializes the new view page + copies items from the old view at the
    start of ``update_workspace``, then runs the multi-phase review. This
    keeps view-creation inside the workspace_update stage rather than ahead
    of build_context, so ``--up-to-stage build_context`` is a clean preview.
    """

    def __init__(self, view_id: str, call_type: CallType) -> None:
        self._view_id = view_id
        self._call_type = call_type
        self._phase_lines: list[str] = []

    async def materialize(self, infra: CallInfra) -> tuple[str, str]:
        """Create the new view page, supersede the old one, and copy
        VIEW_ITEM links across. Returns ``(old_view_id, new_view_id)``.

        Exposed as a public method so tests can exercise the materialization
        step directly without running the full LLM-driven updater.
        """
        db = infra.db
        question_id = infra.question_id
        existing_view = await db.get_view_for_question(question_id)
        if not existing_view:
            raise RuntimeError(
                f"UpdateViewCall requires an existing View for question "
                f"{question_id[:8]}, but none was found."
            )

        question = await db.get_page(question_id)
        q_headline = question.headline if question else question_id[:8]

        new_view = Page(
            id=self._view_id,
            page_type=PageType.VIEW,
            layer=PageLayer.WIKI,
            workspace=Workspace.RESEARCH,
            content="",
            headline=f"View: {q_headline}",
            sections=list(DEFAULT_VIEW_SECTIONS),
            provenance_call_type=self._call_type.value,
            provenance_call_id=infra.call.id,
            provenance_model=get_settings().model,
        )
        await db.save_page(new_view)

        await db.save_link(
            PageLink(
                from_page_id=new_view.id,
                to_page_id=question_id,
                link_type=LinkType.VIEW_OF,
            )
        )

        await db.supersede_page(existing_view.id, new_view.id)

        old_links = await db.get_links_from(existing_view.id)
        copied = 0
        for link in old_links:
            if link.link_type == LinkType.VIEW_ITEM:
                await db.save_link(
                    PageLink(
                        from_page_id=new_view.id,
                        to_page_id=link.to_page_id,
                        link_type=LinkType.VIEW_ITEM,
                        importance=link.importance,
                        section=link.section,
                        position=link.position,
                    )
                )
                copied += 1

        log.info(
            "Created new view %s (superseding %s) with %d copied items",
            new_view.id[:8],
            existing_view.id[:8],
            copied,
        )

        await infra.trace.record_strict(
            ViewCreatedEvent(
                view_id=new_view.id,
                view_headline=new_view.headline,
                question_id=question_id,
                superseded_view_id=existing_view.id,
            )
        )
        return existing_view.id, new_view.id

    async def update_workspace(
        self,
        infra: CallInfra,
        context: ContextResult,
    ) -> UpdateResult:
        await self.materialize(infra)

        sections = _load_prompt_sections()
        system_prompt = _build_update_view_system_prompt(sections.get("context", ""))

        messages: list[dict] = [
            {"role": "user", "content": context.context_text},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "Understood. Ready to review View items.",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
        ]

        created_page_ids: list[str] = []

        messages = await self._phase_score_unscored(infra, system_prompt, sections, messages)

        messages, flagged_ids = await self._phase_triage(infra, system_prompt, sections, messages)

        if flagged_ids:
            messages, phase2b_created = await self._phase_deep_review(
                infra, system_prompt, sections, messages, flagged_ids
            )
            created_page_ids.extend(phase2b_created)

        messages = await self._phase_enforce_caps(infra, system_prompt, sections, messages)

        messages = await self._phase_prune(infra, system_prompt, sections, messages)

        return UpdateResult(
            created_page_ids=created_page_ids,
            moves=[],
            all_loaded_ids=[],
            messages=messages,
            phase_summary="\n".join(self._phase_lines),
        )

    async def _phase_score_unscored(
        self,
        infra: CallInfra,
        system_prompt: str,
        sections: dict[str, str],
        messages: list[dict],
    ) -> list[dict]:
        items = await infra.db.get_view_items(self._view_id)
        unscored = [(page, link) for page, link in items if link.importance is None]

        if not unscored:
            await infra.trace.record(
                PhaseSkippedEvent(phase="score_unscored", reason="No unscored items")
            )
            self._phase_lines.append("score_unscored: skipped (no unscored items)")
            return messages

        link_by_target = {page.id: link for page, link in items}
        page_by_id = {page.id: page for page, link in items}
        batch_sizes = _split_into_batches(len(unscored), SCORE_BATCH_SIZE)
        batch_response_model = pydantic.create_model(
            "UnscoredScoreBatch",
            scores=(
                list[UnscoredItemScore],
                Field(description="One score per item in the batch"),
            ),
        )

        offset = 0
        modified_count = 0
        for batch_idx, batch_size in enumerate(batch_sizes):
            batch = unscored[offset : offset + batch_size]
            offset += batch_size

            item_blocks = []
            for j, (page, link) in enumerate(batch):
                global_idx = sum(batch_sizes[:batch_idx]) + j
                item_blocks.append(
                    f"### Item {global_idx + 1}/{len(unscored)}\n"
                    f"ID: `{page.id[:8]}`\n"
                    f"Headline: {page.headline}\n"
                    f"R{page.robustness} sec={link.section or '?'}\n\n"
                    f"{page.content or '(no content)'}"
                )

            batch_text = (
                f"## Batch {batch_idx + 1}/{len(batch_sizes)} "
                f"({batch_size} items)\n\n"
                + "\n\n".join(item_blocks)
                + "\n\nScore all items in this batch now."
            )

            if batch_idx == 0:
                user_content = sections.get("score_unscored", "") + "\n\n" + batch_text
            else:
                user_content = batch_text

            messages.append({"role": "user", "content": user_content})

            result = await structured_call(
                system_prompt,
                messages=list(messages),
                response_model=batch_response_model,
                cache=True,
                metadata=LLMExchangeMetadata(
                    call_id=infra.call.id,
                    phase=f"score_unscored_batch_{batch_idx}",
                ),
                db=infra.db,
            )

            messages.append({"role": "assistant", "content": result.response_text or ""})

            if result.parsed:
                parsed_dict = result.parsed.model_dump()
                scores = [UnscoredItemScore(**raw) for raw in parsed_dict.get("scores", [])]
                resolved_map = await infra.db.resolve_page_ids([s.item_id for s in scores])
                for score in scores:
                    resolved = resolved_map.get(score.item_id)
                    if not resolved:
                        log.warning("Score target %s not found", score.item_id)
                        continue
                    link = link_by_target.get(resolved)
                    page = page_by_id.get(resolved)
                    if not link or not page:
                        log.warning(
                            "No VIEW_ITEM link from view %s to %s",
                            self._view_id[:8],
                            resolved[:8],
                        )
                        continue
                    changed = await self._apply_item_score(infra.db, score, page, link)
                    if changed:
                        modified_count += 1

        await infra.trace.record(
            UpdateViewPhaseCompletedEvent(
                phase="score_unscored",
                items_processed=len(unscored),
                items_modified=modified_count,
            )
        )
        self._phase_lines.append(
            f"score_unscored: scored {len(unscored)} item(s), modified {modified_count}"
        )
        return messages

    async def _phase_triage(
        self,
        infra: CallInfra,
        system_prompt: str,
        sections: dict[str, str],
        messages: list[dict],
    ) -> tuple[list[dict], list[str]]:
        items = await infra.db.get_view_items(self._view_id)
        scored = [(page, link) for page, link in items if link.importance is not None]

        if not scored:
            await infra.trace.record(PhaseSkippedEvent(phase="triage", reason="No scored items"))
            self._phase_lines.append("triage: skipped (no scored items)")
            return messages, []

        batch_sizes = _split_into_batches(len(scored), TRIAGE_BATCH_SIZE)
        batch_response_model = pydantic.create_model(
            "TriageBatch",
            flags=(
                list[TriageFlag],
                Field(description="One flag per item in the batch"),
            ),
        )

        flagged_ids: list[str] = []
        offset = 0
        for batch_idx, batch_size in enumerate(batch_sizes):
            batch = scored[offset : offset + batch_size]
            offset += batch_size

            compact_lines = [_render_item_compact(page, link) for page, link in batch]
            batch_text = (
                f"## Batch {batch_idx + 1}/{len(batch_sizes)} "
                f"({batch_size} items)\n\n"
                + "\n".join(compact_lines)
                + "\n\nTriage all items in this batch now."
            )

            if batch_idx == 0:
                user_content = sections.get("triage", "") + "\n\n" + batch_text
            else:
                user_content = batch_text

            messages.append({"role": "user", "content": user_content})

            result = await structured_call(
                system_prompt,
                messages=list(messages),
                response_model=batch_response_model,
                cache=True,
                metadata=LLMExchangeMetadata(
                    call_id=infra.call.id,
                    phase=f"triage_batch_{batch_idx}",
                ),
                db=infra.db,
            )

            messages.append({"role": "assistant", "content": result.response_text or ""})

            if result.parsed:
                parsed_dict = result.parsed.model_dump()
                review_ids: list[str] = []
                for raw_flag in parsed_dict.get("flags", []):
                    flag = TriageFlag(**raw_flag)
                    if flag.flag == "review":
                        review_ids.append(flag.item_id)
                if review_ids:
                    resolved_map = await infra.db.resolve_page_ids(review_ids)
                    flagged_ids.extend(resolved_map.values())

        await infra.trace.record(
            UpdateViewPhaseCompletedEvent(
                phase="triage",
                items_processed=len(scored),
                items_modified=len(flagged_ids),
            )
        )
        self._phase_lines.append(
            f"triage: reviewed {len(scored)} item(s), flagged {len(flagged_ids)} for deep review"
        )
        return messages, flagged_ids

    async def _phase_deep_review(
        self,
        infra: CallInfra,
        system_prompt: str,
        sections: dict[str, str],
        messages: list[dict],
        flagged_ids: Sequence[str],
    ) -> tuple[list[dict], list[str]]:
        items = await infra.db.get_view_items(self._view_id)
        flagged_set = set(flagged_ids)
        flagged = [(page, link) for page, link in items if page.id in flagged_set]

        if not flagged:
            await infra.trace.record(
                PhaseSkippedEvent(phase="deep_review", reason="No flagged items found")
            )
            self._phase_lines.append("deep_review: skipped (no flagged items)")
            return messages, []

        link_by_target = {page.id: link for page, link in items}
        page_by_id = {page.id: page for page, link in items}
        flagged_item_ids = [page.id for page, _ in flagged]
        links_by_item = await infra.db.get_links_from_many(flagged_item_ids)
        cited_page_ids: set[str] = set()
        for item_links in links_by_item.values():
            for link in item_links:
                if link.link_type in (LinkType.CITES, LinkType.DEPENDS_ON):
                    cited_page_ids.add(link.to_page_id)
        cited_pages = (
            await infra.db.get_pages_by_ids(list(cited_page_ids)) if cited_page_ids else {}
        )

        parent_considerations = await infra.db.get_considerations_for_question(infra.question_id)
        related_considerations = [claim for claim, _ in parent_considerations]

        batch_sizes = _split_into_batches(len(flagged), DEEP_REVIEW_BATCH_SIZE)
        created_page_ids: list[str] = []
        adjust_count = 0
        supersede_count = 0
        offset = 0

        for batch_idx, batch_size in enumerate(batch_sizes):
            batch = flagged[offset : offset + batch_size]
            offset += batch_size

            item_blocks = []
            for page, link in batch:
                item_links = links_by_item.get(page.id, [])
                item_blocks.append(
                    _render_item_full(
                        page,
                        link,
                        cited_pages,
                        item_links,
                        related_considerations=related_considerations,
                    )
                )

            batch_text = (
                f"## Batch {batch_idx + 1}/{len(batch_sizes)} "
                f"({batch_size} items for deep review)\n\n"
                + "\n\n---\n\n".join(item_blocks)
                + "\n\nReview all items in this batch. "
                "You may also propose new items if you notice gaps."
            )

            if batch_idx == 0:
                user_content = sections.get("deep_review", "") + "\n\n" + batch_text
            else:
                user_content = batch_text

            messages.append({"role": "user", "content": user_content})

            result = await structured_call(
                system_prompt,
                messages=list(messages),
                response_model=DeepReviewBatchResponse,
                cache=True,
                metadata=LLMExchangeMetadata(
                    call_id=infra.call.id,
                    phase=f"deep_review_batch_{batch_idx}",
                ),
                db=infra.db,
            )

            messages.append({"role": "assistant", "content": result.response_text or ""})

            if result.parsed:
                resolved_map = await infra.db.resolve_page_ids(
                    [r.item_id for r in result.parsed.item_reviews]
                )
                for review in result.parsed.item_reviews:
                    resolved = resolved_map.get(review.item_id)
                    if not resolved:
                        log.warning("Review target %s not found", review.item_id)
                        continue
                    page = page_by_id.get(resolved)
                    link = link_by_target.get(resolved)
                    if not page:
                        log.warning(
                            "Page %s not found in view items",
                            resolved[:8],
                        )
                        continue
                    changed = await self._apply_item_review(infra, review, resolved, page, link)
                    if changed:
                        if review.action == "supersede":
                            supersede_count += 1
                        elif review.action == "adjust":
                            adjust_count += 1
                    if review.action == "supersede" and resolved in link_by_target:
                        del link_by_target[resolved]

                for proposal in result.parsed.proposed_items:
                    new_id = await self._create_proposed_item(infra, proposal)
                    if new_id:
                        created_page_ids.append(new_id)

        await infra.trace.record(
            UpdateViewPhaseCompletedEvent(
                phase="deep_review",
                items_processed=len(flagged),
                items_modified=adjust_count + supersede_count,
                items_created=len(created_page_ids),
            )
        )
        self._phase_lines.append(
            f"deep_review: reviewed {len(flagged)} item(s), "
            f"superseded {supersede_count}, adjusted {adjust_count}, "
            f"proposed {len(created_page_ids)} new item(s)"
        )
        return messages, created_page_ids

    async def _phase_enforce_caps(
        self,
        infra: CallInfra,
        system_prompt: str,
        sections: dict[str, str],
        messages: list[dict],
    ) -> list[dict]:
        settings = get_settings()
        caps = {
            5: settings.view_importance_5_cap,
            4: settings.view_importance_4_cap,
            3: settings.view_importance_3_cap,
            2: settings.view_importance_2_cap,
        }

        any_enforced = False
        first_call = True
        total_demotions = 0
        for level in [5, 4, 3, 2]:
            items = await infra.db.get_view_items(self._view_id)
            at_level = [
                (p, l) for p, l in items if l.importance is not None and l.importance == level
            ]
            cap = caps[level]

            if len(at_level) <= cap:
                continue

            any_enforced = True
            excess = len(at_level) - cap

            compact_lines = [_render_item_compact(page, link) for page, link in at_level]
            batch_text = (
                f"## Importance {level}: {len(at_level)} items, cap is {cap} "
                f"(need to demote {excess})\n\n"
                + "\n".join(compact_lines)
                + f"\n\nChoose {excess} item(s) to demote below importance {level}."
            )

            if first_call:
                user_content = sections.get("enforce_caps", "") + "\n\n" + batch_text
                first_call = False
            else:
                user_content = batch_text

            demotion_response_model = pydantic.create_model(
                f"DemotionBatch_I{level}",
                demotions=(
                    list[DemotionChoice],
                    Field(description="Items to demote"),
                ),
            )

            messages.append({"role": "user", "content": user_content})

            result = await structured_call(
                system_prompt,
                messages=list(messages),
                response_model=demotion_response_model,
                cache=True,
                metadata=LLMExchangeMetadata(
                    call_id=infra.call.id,
                    phase=f"enforce_caps_i{level}",
                ),
                db=infra.db,
            )

            messages.append({"role": "assistant", "content": result.response_text or ""})

            if result.parsed:
                parsed_dict = result.parsed.model_dump()
                demotions = [DemotionChoice(**raw) for raw in parsed_dict.get("demotions", [])]
                link_by_page = {p.id: l for p, l in at_level}
                resolved_map = await infra.db.resolve_page_ids([d.item_id for d in demotions])
                for demotion in demotions:
                    resolved = resolved_map.get(demotion.item_id)
                    if not resolved:
                        log.warning(
                            "Demotion target %s not found",
                            demotion.item_id,
                        )
                        continue
                    link = link_by_page.get(resolved)
                    if not link:
                        log.warning(
                            "No VIEW_ITEM link from view %s to %s",
                            self._view_id[:8],
                            resolved[:8],
                        )
                        continue
                    await self._apply_demotion(infra.db, demotion, resolved, link)
                    total_demotions += 1

        if not any_enforced:
            await infra.trace.record(
                PhaseSkippedEvent(
                    phase="enforce_caps",
                    reason="All importance levels within caps",
                )
            )
            self._phase_lines.append("enforce_caps: skipped (all levels within caps)")
        else:
            self._phase_lines.append(
                f"enforce_caps: demoted {total_demotions} item(s) to respect importance caps"
            )

        return messages

    async def _phase_prune(
        self,
        infra: CallInfra,
        system_prompt: str,
        sections: dict[str, str],
        messages: list[dict],
    ) -> list[dict]:
        items = await infra.db.get_view_items(self._view_id)
        candidates = _prune_candidates(items)

        if not candidates:
            await infra.trace.record(PhaseSkippedEvent(phase="prune", reason="No prune candidates"))
            self._phase_lines.append("prune: skipped (no prune candidates)")
            return messages

        dup_count = sum(1 for p, _ in candidates if _is_explicit_duplicate(p))

        link_by_target = {page.id: link for page, link in items}

        compact_lines = [_render_item_compact(page, link) for page, link in candidates]
        header = (
            f"## Prune candidates ({len(candidates)} items — "
            f"low-importance plus {dup_count} marked as duplicates)"
            if dup_count
            else f"## Prune candidates ({len(candidates)} items)"
        )
        batch_text = (
            header
            + "\n\n"
            + "\n".join(compact_lines)
            + "\n\nDecide which items to keep and which to remove. "
            "Items whose content explicitly marks them as duplicates "
            "(e.g. '[Duplicate of ...]') should almost always be removed."
        )

        user_content = sections.get("prune", "") + "\n\n" + batch_text

        prune_response_model = pydantic.create_model(
            "PruneBatch",
            decisions=(
                list[PruneDecision],
                Field(description="One decision per item"),
            ),
        )

        messages.append({"role": "user", "content": user_content})

        result = await structured_call(
            system_prompt,
            messages=list(messages),
            response_model=prune_response_model,
            cache=True,
            metadata=LLMExchangeMetadata(
                call_id=infra.call.id,
                phase="prune",
            ),
            db=infra.db,
        )

        messages.append({"role": "assistant", "content": result.response_text or ""})

        removed = 0
        if result.parsed:
            parsed_dict = result.parsed.model_dump()
            decisions = [PruneDecision(**raw) for raw in parsed_dict.get("decisions", [])]
            remove_decisions = [d for d in decisions if d.action == "remove"]
            if remove_decisions:
                resolved_map = await infra.db.resolve_page_ids(
                    [d.item_id for d in remove_decisions]
                )
                for decision in remove_decisions:
                    resolved = resolved_map.get(decision.item_id)
                    if not resolved:
                        log.warning("Prune target %s not found", decision.item_id)
                        continue
                    link = link_by_target.get(resolved)
                    if not link:
                        continue
                    did_remove = await self._unlink_item(infra.db, resolved, link)
                    if did_remove:
                        removed += 1

        await infra.trace.record(
            UpdateViewPhaseCompletedEvent(
                phase="prune",
                items_processed=len(candidates),
                items_removed=removed,
            )
        )
        self._phase_lines.append(
            f"prune: considered {len(candidates)} item(s)"
            + (f" ({dup_count} marked duplicate)" if dup_count else "")
            + f", removed {removed}"
        )
        return messages

    async def _apply_item_score(
        self, db: DB, score: UnscoredItemScore, page: Page, link: PageLink
    ) -> bool:
        """Apply importance/section scores to a VIEW_ITEM link. Returns True if changed."""
        link.importance = score.importance
        link.section = score.section
        await db.save_link(link)

        if score.robustness is not None:
            await db.update_epistemic_score(
                page.id,
                robustness=score.robustness,
                robustness_reasoning=score.robustness_reasoning,
            )

        return True

    async def _apply_item_review(
        self,
        infra: CallInfra,
        review: ItemReview,
        resolved_id: str,
        page: Page,
        link: PageLink | None,
    ) -> bool:
        """Apply a deep review decision. Returns True if item was modified."""
        if review.action == "keep":
            return False

        if review.action == "adjust":
            if link:
                if review.new_importance is not None:
                    link.importance = review.new_importance
                if review.new_section is not None:
                    link.section = review.new_section
                await infra.db.save_link(link)

            if review.new_robustness is not None:
                await infra.db.update_epistemic_score(
                    page.id,
                    robustness=review.new_robustness,
                    robustness_reasoning=review.new_robustness_reasoning,
                )
            return True

        if review.action == "supersede":
            return await self._supersede_item(infra, page, review, link)

        return False

    async def _supersede_item(
        self,
        infra: CallInfra,
        old_page: Page,
        review: ItemReview,
        old_link: PageLink | None,
    ) -> bool:
        """Create a new VIEW_ITEM superseding the old one."""
        new_page = Page(
            page_type=PageType.VIEW_ITEM,
            layer=PageLayer.WIKI,
            workspace=Workspace.RESEARCH,
            content=review.new_content or old_page.content,
            headline=review.new_headline or old_page.headline,
            robustness=review.new_robustness or old_page.robustness,
            robustness_reasoning=(review.new_robustness_reasoning or old_page.robustness_reasoning),
            provenance_model=get_settings().model,
            provenance_call_type=infra.call.call_type.value,
            provenance_call_id=infra.call.id,
        )
        await infra.db.save_page(new_page)

        try:
            await embed_and_store_page(infra.db, new_page, field_name="abstract")
        except Exception:
            log.warning("Failed to embed new view item %s", new_page.id[:8], exc_info=True)
        try:
            await extract_and_link_citations(
                new_page.id, new_page.content, infra.db, call=infra.call
            )
        except Exception:
            log.warning(
                "Citation extraction failed for %s",
                new_page.id[:8],
                exc_info=True,
            )

        await infra.db.supersede_page(old_page.id, new_page.id)

        importance = review.new_importance
        section = review.new_section
        if old_link:
            if importance is None:
                importance = old_link.importance
            if section is None:
                section = old_link.section
            await infra.db.delete_link(old_link.id)

        await infra.db.save_link(
            PageLink(
                from_page_id=self._view_id,
                to_page_id=new_page.id,
                link_type=LinkType.VIEW_ITEM,
                importance=importance,
                section=section or "other",
                position=old_link.position if old_link else 0,
            )
        )

        log.info(
            "Superseded view item %s -> %s",
            old_page.id[:8],
            new_page.id[:8],
        )
        return True

    async def _create_proposed_item(self, infra: CallInfra, proposal: ProposedItem) -> str | None:
        """Create a new VIEW_ITEM from a proposal and link it to the View."""
        new_page = Page(
            page_type=PageType.VIEW_ITEM,
            layer=PageLayer.WIKI,
            workspace=Workspace.RESEARCH,
            content=proposal.content,
            headline=proposal.headline,
            robustness=proposal.robustness,
            robustness_reasoning=proposal.robustness_reasoning,
            provenance_model=get_settings().model,
            provenance_call_type=infra.call.call_type.value,
            provenance_call_id=infra.call.id,
        )
        await infra.db.save_page(new_page)

        try:
            await embed_and_store_page(infra.db, new_page, field_name="abstract")
        except Exception:
            log.warning(
                "Failed to embed proposed item %s",
                new_page.id[:8],
                exc_info=True,
            )
        try:
            await extract_and_link_citations(
                new_page.id, new_page.content, infra.db, call=infra.call
            )
        except Exception:
            log.warning(
                "Citation extraction failed for %s",
                new_page.id[:8],
                exc_info=True,
            )

        existing_links = await infra.db.get_links_from(self._view_id)
        section_positions = [
            link.position or 0
            for link in existing_links
            if link.link_type == LinkType.VIEW_ITEM and link.section == proposal.section
        ]
        next_position = max(section_positions, default=-1) + 1

        await infra.db.save_link(
            PageLink(
                from_page_id=self._view_id,
                to_page_id=new_page.id,
                link_type=LinkType.VIEW_ITEM,
                importance=proposal.importance,
                section=proposal.section,
                position=next_position,
            )
        )

        log.info(
            "Created proposed view item %s (I%d, sec=%s)",
            new_page.id[:8],
            proposal.importance,
            proposal.section,
        )
        return new_page.id

    async def _apply_demotion(
        self, db: DB, demotion: DemotionChoice, resolved_id: str, link: PageLink
    ) -> None:
        """Lower an item's importance score."""
        link.importance = demotion.new_importance
        await db.save_link(link)
        log.info(
            "Demoted %s to I%d: %s",
            resolved_id[:8],
            demotion.new_importance,
            demotion.reasoning[:80],
        )

    async def _unlink_item(self, db: DB, resolved_id: str, link: PageLink) -> bool:
        """Remove a VIEW_ITEM link from the View. Returns True if unlinked."""
        await db.delete_link(link.id)
        log.info("Unlinked view item %s from view", resolved_id[:8])
        return True


class UpdateViewCall(CallRunner):
    """Incrementally update an existing View page for a question."""

    context_builder_cls = UpdateViewContext  # type: ignore[assignment]
    workspace_updater_cls = UpdateViewWorkspaceUpdater  # type: ignore[assignment]
    closing_reviewer_cls = ViewClosingReview  # type: ignore[assignment]
    call_type = CallType.UPDATE_VIEW

    def __init__(self, question_id: str, call: Call, db: DB, **kwargs) -> None:
        # Mint the new view UUID up front so factories can bind to it. The
        # actual save_page + supersede + link-copy runs at update_workspace
        # time (inside UpdateViewWorkspaceUpdater.materialize), so
        # --up-to-stage build_context doesn't mutate the workspace.
        self._view_id: str = str(uuid.uuid4())
        super().__init__(question_id, call, db, **kwargs)

    def _make_context_builder(self) -> ContextBuilder:
        return UpdateViewContext()

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return UpdateViewWorkspaceUpdater(self._view_id, self.call_type)

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return ViewClosingReview(self.call_type, view_id=self._view_id)

    def task_description(self) -> str:
        return (
            "Incrementally update the View for this question.\n\n"
            f"Question ID: `{self.infra.question_id}`\n"
            f"View ID: `{self._view_id}`\n"
        )
