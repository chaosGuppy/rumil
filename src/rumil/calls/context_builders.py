"""ContextBuilder implementations for all call types."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence

from pydantic import BaseModel, Field

from rumil.calls.common import (
    ABSTRACT_INSTRUCTION,
    PageSummaryItem,
    resolve_page_refs,
    save_page_abstracts,
)
from rumil.calls.stages import CallInfra, ContextBuilder, ContextResult
from rumil.context import (
    build_embedding_based_context,
    format_page,
)
from rumil.database import DB
from rumil.embeddings import page_query_text, search_pages
from rumil.llm import (
    LLMExchangeMetadata,
    build_system_prompt,
    build_user_message,
    structured_call,
)
from rumil.models import (
    CallType,
    LinkType,
    Page,
    PageDetail,
    PageLink,
    PageType,
)
from rumil.settings import get_settings
from rumil.tracing.trace_events import ContextBuiltEvent

log = logging.getLogger(__name__)

_B2_SEMAPHORE = asyncio.Semaphore(15)


async def _record_context_built(
    infra: CallInfra,
    working_page_ids: Sequence[str],
    preloaded_ids: Sequence[str],
    *,
    source_page_id: str | None = None,
) -> None:
    await infra.trace.record(
        ContextBuiltEvent(
            working_context_page_ids=await resolve_page_refs(working_page_ids, infra.db),
            preloaded_page_ids=await resolve_page_refs(preloaded_ids, infra.db),
            source_page_id=source_page_id,
        )
    )


class CreateViewContext(ContextBuilder):
    """Context for View creation: loads the question, all its considerations,
    judgements, child question judgements, and any unscored View item proposals."""

    async def build_context(self, infra: CallInfra) -> ContextResult:
        question = await infra.db.get_page(infra.question_id)
        query = question.headline if question else infra.question_id

        result = await build_embedding_based_context(
            query,
            infra.db,
            scope_question_id=infra.question_id,
            require_judgement_for_questions=True,
        )
        working_page_ids = result.page_ids
        preloaded_ids = list(infra.call.context_page_ids or [])

        context_text = result.context_text

        existing_view = await infra.db.get_view_for_question(infra.question_id)
        if existing_view:
            items = await infra.db.get_view_items(existing_view.id)
            if items:
                parts = ["\n\n---\n\n## Existing View Items (to update/supersede)\n"]
                for page, link in items:
                    imp = f"I{link.importance}" if link.importance else "unscored"
                    formatted = await format_page(
                        page,
                        PageDetail.CONTENT,
                        linked_detail=None,
                        db=infra.db,
                        track=True,
                        track_tags={"source": "existing_view_items"},
                    )
                    parts.append(
                        f"\n### [{page.page_type.value.upper()} R{page.robustness} {imp}] "
                        f"`{page.id[:8]}` — {page.headline}\n\n"
                        f"{formatted}\n"
                    )
                context_text += "\n".join(parts)

        if preloaded_ids:
            pages_by_id = await infra.db.get_pages_by_ids(preloaded_ids)
            parts_pre: list[str] = []
            for pid in preloaded_ids:
                page = pages_by_id.get(pid)
                if page:
                    parts_pre += [
                        "",
                        "---",
                        "",
                        f"## Pre-loaded Page: `{pid[:8]}`",
                        "",
                        await format_page(
                            page,
                            PageDetail.CONTENT,
                            db=infra.db,
                            track=True,
                            track_tags={"source": "preloaded"},
                        ),
                    ]
            context_text += "\n".join(parts_pre)

        await _record_context_built(infra, working_page_ids, preloaded_ids)
        return ContextResult(
            context_text=context_text,
            working_page_ids=working_page_ids,
            preloaded_ids=preloaded_ids,
        )


class EmbeddingContext(ContextBuilder):
    """Embedding-based context. Used by embedding variants."""

    def __init__(
        self,
        call_type: CallType,
        *,
        require_judgement_for_questions: bool = False,
    ) -> None:
        self._call_type = call_type
        self._require_judgement_for_questions = require_judgement_for_questions

    async def build_context(self, infra: CallInfra) -> ContextResult:
        question = await infra.db.get_page(infra.question_id)
        query = question.headline if question else infra.question_id
        result = await build_embedding_based_context(
            query,
            infra.db,
            scope_question_id=infra.question_id,
            require_judgement_for_questions=self._require_judgement_for_questions,
        )
        working_page_ids = result.page_ids
        preloaded_ids = infra.call.context_page_ids or []

        context_text = result.context_text
        if preloaded_ids:
            parts: list[str] = []
            for pid in preloaded_ids:
                page = await infra.db.get_page(pid)
                if page:
                    parts += [
                        "",
                        "---",
                        "",
                        f"## Pre-loaded Page: `{pid[:8]}`",
                        "",
                        await format_page(
                            page,
                            PageDetail.CONTENT,
                            db=infra.db,
                            track=True,
                            track_tags={"source": "preloaded"},
                        ),
                    ]
            context_text += "\n".join(parts)

        await _record_context_built(infra, working_page_ids, preloaded_ids)
        return ContextResult(
            context_text=context_text,
            working_page_ids=working_page_ids,
            preloaded_ids=preloaded_ids,
        )


_SIBLING_BLOCK_HEADER = (
    "## Existing child questions of this parent\n\n"
    "The parent question already has these direct child questions. Any new "
    "child question you create must be INDEPENDENT of these existing "
    "siblings: its impact on the parent question must NOT be largely "
    "mediated through any of them. If a candidate's contribution to "
    "resolving the parent question flows mostly via an existing child's "
    "answer, that candidate is not independent — do not create it.\n\n"
    "Independence is stronger than non-duplication. Two siblings can have "
    "different wordings and still fail independence: if answering one "
    "largely determines the other's impact on the parent, they are not "
    "independent. Judge by the causal path of the answer's influence on "
    "the parent, not by surface similarity.\n"
)


async def _format_sibling_children_block(
    scope_question_id: str,
    db: DB,
) -> str:
    """Return a formatted block listing direct child questions of the scope question.

    Empty string when the scope question has no active child questions. Uses
    batched queries: one for links+pages (via get_child_questions_with_links)
    and one for judgements across all children.
    """
    children = await db.get_child_questions_with_links(scope_question_id)
    if not children:
        return ""

    judgements_by_qid = await db.get_judgements_for_questions([child.id for child, _ in children])

    lines: list[str] = [_SIBLING_BLOCK_HEADER]
    for child, link in children:
        scout_tag = child.provenance_call_type or "unknown"
        impact = link.impact_on_parent_question
        impact_str = f"{impact}/10" if impact is not None else "unset"
        judgements = judgements_by_qid.get(child.id, [])
        if judgements:
            latest = max(judgements, key=lambda j: j.created_at)
            cred = latest.credence if latest.credence is not None else "?"
            rob = latest.robustness if latest.robustness is not None else "?"
            judgement_str = f"C{cred}/R{rob}"
        else:
            judgement_str = "none yet"
        lines.append(
            f"- [{scout_tag}] `{child.id[:8]}` — {child.headline}\n"
            f"  (impact_on_parent: {impact_str}; judgement: {judgement_str})"
        )
    return "\n".join(lines) + "\n"


class ScoutSiblingAwareContext(EmbeddingContext):
    """Embedding context with a prepended block listing existing direct child
    questions of the scope question, so the scout can avoid creating children
    whose impact on the parent is mediated through an existing sibling.
    """

    async def build_context(self, infra: CallInfra) -> ContextResult:
        result = await super().build_context(infra)
        sibling_block = await _format_sibling_children_block(
            infra.question_id,
            infra.db,
        )
        if not sibling_block:
            return result
        prepended = f"{sibling_block}\n---\n\n{result.context_text}"
        return ContextResult(
            context_text=prepended,
            working_page_ids=result.working_page_ids,
            preloaded_ids=result.preloaded_ids,
        )


class IngestEmbeddingContext(ContextBuilder):
    """Embedding-based context with source document. Used by IngestCall."""

    def __init__(self, source_page: Page) -> None:
        self._source_page = source_page
        extra = source_page.extra or {}
        self._filename = extra.get("filename", source_page.id[:8])

    async def build_context(self, infra: CallInfra) -> ContextResult:
        question = await infra.db.get_page(infra.question_id)
        query = question.headline if question else infra.question_id
        result = await build_embedding_based_context(
            query,
            infra.db,
            scope_question_id=infra.question_id,
        )
        working_page_ids = result.page_ids
        await _record_context_built(
            infra,
            working_page_ids,
            [],
            source_page_id=self._source_page.id,
        )

        formatted_source = await format_page(
            self._source_page,
            PageDetail.CONTENT,
            linked_detail=None,
            db=infra.db,
            track=True,
            track_tags={"source": "source_document"},
        )
        source_section = (
            f"\n\n---\n\n## Source Document\n\n**File:** {self._filename}\n\n{formatted_source}"
        )
        return ContextResult(
            context_text=result.context_text + source_section,
            working_page_ids=working_page_ids,
        )


class WebResearchEmbeddingContext(ContextBuilder):
    """Embedding context for web research. Records diagnostic info."""

    async def build_context(self, infra: CallInfra) -> ContextResult:
        question = await infra.db.get_page(infra.question_id)
        query = question.headline if question else infra.question_id
        emb_result = await build_embedding_based_context(
            query,
            infra.db,
            scope_question_id=infra.question_id,
        )
        working_page_ids = emb_result.page_ids
        context_text = emb_result.context_text

        system_prompt = build_system_prompt("web_research")
        user_message = build_user_message(context_text, "(diagnostic)")
        log.debug(
            "Web research context diagnostic: "
            "context_text=%d chars, system_prompt=%d chars, "
            "user_message=%d chars, total_prompt=%d chars, "
            "full_pages=%d, abstract_pages=%d, summary_pages=%d, "
            "distillation_pages=%d, "
            "budget_usage=%s",
            len(context_text),
            len(system_prompt),
            len(user_message),
            len(system_prompt) + len(user_message),
            len(emb_result.full_page_ids),
            len(emb_result.abstract_page_ids),
            len(emb_result.summary_page_ids),
            len(emb_result.distillation_page_ids),
            emb_result.budget_usage,
        )

        await infra.trace.record(
            ContextBuiltEvent(
                working_context_page_ids=await resolve_page_refs(
                    working_page_ids,
                    infra.db,
                ),
                preloaded_page_ids=[],
            )
        )

        return ContextResult(
            context_text=context_text,
            working_page_ids=working_page_ids,
        )


class _PageSelection(BaseModel):
    page_ids: list[str] = Field(description="8-char short IDs of the selected pages")


async def _select_sensitive_pages(
    pages: Sequence[Page],
    question: Page,
    latest_judgement: Page | None,
    limit: int,
    infra: CallInfra,
) -> list[Page]:
    """Ask an LLM which pages the judgement is most sensitive to.

    Formats each page with its headline and citation excerpts from the
    judgement, then asks the model to select up to *limit* pages.
    Returns the selected subset in the original order.
    """
    judgement_content = latest_judgement.content if latest_judgement else None

    page_entries: list[str] = []
    for p in pages:
        entry = f"### `{p.id[:8]}` — {p.headline} ({p.page_type.value})"
        if judgement_content:
            cite_pattern = re.compile(rf"[^\n]*\[{re.escape(p.id[:8])}\][^\n]*")
            cite_lines = cite_pattern.findall(judgement_content)
            if cite_lines:
                entry += "\n\nCited in the judgement as follows:"
                for line in cite_lines[:5]:
                    entry += f"\n> {line.strip()}"
            else:
                entry += "\n\n(Not directly cited in the judgement.)"
        page_entries.append(entry)

    page_list = "\n\n".join(page_entries)

    user_message_parts = [f"## Question\n\n**{question.headline}**"]
    if latest_judgement:
        user_message_parts.append(
            f"## Judgement\n\n**{latest_judgement.headline}**\n\n{latest_judgement.content}"
        )
    user_message_parts.append(
        "## Candidate pages\n\n"
        f"Pick the {limit} pages whose content the judgement is most "
        f"sensitive to.\n\n{page_list}"
    )

    meta = LLMExchangeMetadata(
        call_id=infra.call.id,
        phase="big_assess_select_sensitive",
    )
    result = await structured_call(
        "You are deciding which pages are most important for a research "
        "question's judgement.\n\n"
        "A page is important if the judgement is **sensitive** to its "
        "content — meaning that if the page's content changed, the "
        "judgement's conclusions would likely change too. A page that "
        "is cited heavily, or that supplies key numbers, thresholds, "
        "or pivotal reasoning to the judgement, is more important than "
        "one that is mentioned in passing or provides background colour.\n\n"
        "You cannot see each page's full content, so judge importance "
        "from the page's headline and how it is cited in the judgement.\n\n"
        f"Select up to {limit} pages. Return their 8-char short IDs.",
        user_message="\n\n".join(user_message_parts),
        response_model=_PageSelection,
        metadata=meta,
        db=infra.db,
    )
    if result.parsed:
        selected_ids = set(result.parsed.page_ids)
        return [p for p in pages if p.id[:8] in selected_ids][:limit]
    return list(pages[:limit])


class _ReplacementPick(BaseModel):
    replacement_ids: list[str] = Field(
        default_factory=list,
        description=(
            "8-char short IDs of candidates that are more robust, credible, "
            "or useful versions of the target page. Empty list if none qualify."
        ),
    )


_CONNECTED_LINK_TYPES = {
    LinkType.CONSIDERATION,
    LinkType.CHILD_QUESTION,
    LinkType.RELATED,
    LinkType.ANSWERS,
    LinkType.DEPENDS_ON,
}


async def _gather_connected_pages(
    page_id: str,
    db: DB,
) -> list[tuple[Page, PageLink]]:
    """Return all (page, link) pairs directly connected to a page.

    Gathers considerations, child questions, and judgements linked to *page_id*.
    Includes superseded pages so that supersession resolution can detect and swap them.
    """
    links_to = await db.get_links_to(page_id)
    links_from = await db.get_links_from(page_id)

    pairs: list[tuple[str, PageLink]] = []
    for link in links_to:
        if link.link_type in _CONNECTED_LINK_TYPES:
            pairs.append((link.from_page_id, link))
    for link in links_from:
        if link.link_type in _CONNECTED_LINK_TYPES:
            pairs.append((link.to_page_id, link))

    if not pairs:
        return []

    all_ids = list({pid for pid, _ in pairs})
    pages = await db.get_pages_by_ids(all_ids)

    results: list[tuple[Page, PageLink]] = []
    for pid, link in pairs:
        page = pages.get(pid)
        if page:
            results.append((page, link))

    return results


async def _swap_superseded_link(
    page: Page,
    link: PageLink,
    new_page: Page,
    db: DB,
) -> PageLink:
    """Delete *link* and create a replacement pointing to/from *new_page*.

    If an equivalent link (same endpoints, type, and direction) already exists
    on *new_page*, the old link is simply deleted and the existing one returned.

    Returns the new or pre-existing link.
    """
    await db.delete_link(link.id)
    if link.from_page_id == page.id:
        new_from = new_page.id
        new_to = link.to_page_id
    else:
        new_from = link.from_page_id
        new_to = new_page.id

    existing_links = await db.get_links_from(new_from)
    for existing in existing_links:
        if (
            existing.to_page_id == new_to
            and existing.link_type == link.link_type
            and existing.direction == link.direction
        ):
            log.info(
                "Superseded link %s: equivalent link already exists on %s -> %s, skipping creation",
                link.id[:8],
                new_from[:8],
                new_to[:8],
            )
            return existing

    new_link = PageLink(
        from_page_id=new_from,
        to_page_id=new_to,
        link_type=link.link_type,
        direction=link.direction,
        strength=link.strength,
        reasoning=link.reasoning,
        role=link.role,
    )
    await db.save_link(new_link)
    log.info(
        "Swapped superseded link %s: %s -> %s (page %s -> %s)",
        link.id[:8],
        link.from_page_id[:8],
        link.to_page_id[:8],
        page.id[:8],
        new_page.id[:8],
    )
    return new_link


async def _get_latest_judgement(
    question_id: str,
    db: DB,
) -> Page | None:
    """Return the most recent active judgement for a question, or None."""
    judgements = await db.get_judgements_for_question(question_id)
    if not judgements:
        return None
    return max(judgements, key=lambda j: j.created_at)


async def _resolve_superseded_connections(
    question_id: str,
    latest_judgement: Page | None,
    db: DB,
) -> list[tuple[Page, PageLink]]:
    """Resolve superseded connected pages by swapping links.

    Returns the refreshed list of connected (page, link) pairs.
    """
    target_ids = [question_id]
    if latest_judgement:
        target_ids.append(latest_judgement.id)

    connected: list[tuple[Page, PageLink]] = []
    for tid in target_ids:
        connected.extend(await _gather_connected_pages(tid, db))

    seen_link_ids: set[str] = set()
    deduped: list[tuple[Page, PageLink]] = []
    for page, link in connected:
        if link.id not in seen_link_ids:
            seen_link_ids.add(link.id)
            deduped.append((page, link))
    connected = deduped

    # Batch-resolve all superseded pages in one call rather than N
    # singular chain walks. Without batching this loop is the hottest
    # N+1 in the call-type context builders — see chaosGuppy/rumil#275.
    superseded_ids = [page.id for page, _link in connected if page.is_superseded]
    replacements: dict[str, Page] = {}
    if superseded_ids:
        replacements = await db.resolve_supersession_chains(superseded_ids)

    refreshed: list[tuple[Page, PageLink]] = []
    for page, link in connected:
        if page.is_superseded:
            new_page = replacements.get(page.id)
            if new_page:
                new_link = await _swap_superseded_link(page, link, new_page, db)
                refreshed.append((new_page, new_link))
            else:
                refreshed.append((page, link))
        else:
            refreshed.append((page, link))

    return refreshed


_CITATION_RE = re.compile(r"\[([a-f0-9]{8})\]")


async def _cites_superseded_pages(page: Page, db: DB) -> bool:
    """Check whether *page*'s inline citations reference any superseded page."""
    if not page.content:
        return False
    short_ids = set(_CITATION_RE.findall(page.content))
    if not short_ids:
        return False
    resolved_map = await db.resolve_page_ids(list(short_ids))
    full_ids = list(resolved_map.values())
    if not full_ids:
        return False
    cited_pages = await db.get_pages_by_ids(full_ids)
    return any(p.is_superseded for p in cited_pages.values())


async def _reassess_pages_citing_superseded(
    connected: Sequence[tuple[Page, PageLink]],
    question: Page,
    latest_judgement: Page | None,
    infra: CallInfra,
) -> None:
    """Find connected pages that cite superseded content and reassess them."""
    stale: list[Page] = []
    checks = await asyncio.gather(
        *[_cites_superseded_pages(page, infra.db) for page, _ in connected]
    )
    for (page, _), is_stale in zip(connected, checks):
        if is_stale:
            stale.append(page)

    if not stale:
        log.info("Reassess stale: no connected pages cite superseded pages")
        return

    log.info("Reassess stale: %d connected pages cite superseded pages", len(stale))

    if len(stale) > 5:
        stale = await _select_sensitive_pages(
            stale,
            question,
            latest_judgement,
            limit=5,
            infra=infra,
        )

    async def _do_reassess(page: Page) -> None:
        from rumil.clean.common import reassess_claim, reassess_question

        if page.page_type == PageType.QUESTION:
            await reassess_question(
                page.id,
                [],
                infra.call,
                infra.db,
                infra.trace,
                assess_variant="default",
            )
        elif page.page_type == PageType.JUDGEMENT:
            links = await infra.db.get_links_from(page.id)
            question_links = [l for l in links if l.link_type == LinkType.ANSWERS]
            if question_links:
                question_id = question_links[0].to_page_id
                log.info(
                    "Stale judgement %s: reassessing its question %s",
                    page.id[:8],
                    question_id[:8],
                )
                await reassess_question(
                    question_id,
                    [],
                    infra.call,
                    infra.db,
                    infra.trace,
                    assess_variant="default",
                )
            else:
                log.warning(
                    "Stale judgement %s has no linked question, skipping",
                    page.id[:8],
                )
        else:
            await reassess_claim(
                page.id,
                "",
                infra.call,
                infra.db,
                infra.trace,
            )

    log.info("Reassess stale: reassessing %d pages", len(stale))
    await asyncio.gather(*[_do_reassess(p) for p in stale])


async def _find_higher_quality_replacements(
    connected: Sequence[tuple[Page, PageLink]],
    question: Page,
    latest_judgement: Page | None,
    infra: CallInfra,
) -> set[str]:
    """Search for higher-quality replacements for connected pages via embeddings.

    For each connected page, searches for similar pages by embedding distance.
    If more than 10 connected pages have matches, asks an LLM which 10 are
    most important to the question. For each chosen connected page, asks an
    LLM whether any matches are more robust, credible, or useful versions.

    Returns a set of page IDs chosen as better replacements (for context only).
    """
    connected_ids = {page.id for page, _ in connected} | {infra.question_id}
    scope_links = await infra.db.get_links_to(infra.question_id)
    scope_linked_ids = {l.from_page_id for l in scope_links} | {
        l.to_page_id for l in await infra.db.get_links_from(infra.question_id)
    }
    exclude_ids = connected_ids | scope_linked_ids

    async def _search_for_page(page: Page) -> tuple[Page, list[tuple[Page, float]]]:
        async with _B2_SEMAPHORE:
            results = await search_pages(
                infra.db,
                page_query_text(page),
                match_threshold=0.85,
                match_count=5,
                input_type="document",
            )
        filtered = [(p, score) for p, score in results if p.id not in exclude_ids and p.is_active()]
        return page, filtered

    search_results = await asyncio.gather(*[_search_for_page(page) for page, _ in connected])

    for page, candidates in search_results:
        if candidates:
            match_titles = ", ".join(f"{c.headline[:40]} ({score:.2f})" for c, score in candidates)
            log.info(
                "Find replacements: %s (%s) — %d matches: %s",
                page.id[:8],
                page.headline[:50],
                len(candidates),
                match_titles,
            )
        else:
            log.info(
                "Find replacements: %s (%s) — no matches",
                page.id[:8],
                page.headline[:50],
            )

    pages_with_matches = [(page, candidates) for page, candidates in search_results if candidates]

    if not pages_with_matches:
        log.info("Find replacements: no connected pages have similar matches")
        return set()

    log.info(
        "Phase B2: %d connected pages have embedding matches",
        len(pages_with_matches),
    )

    if len(pages_with_matches) > 10:
        match_pages = [p for p, _ in pages_with_matches]
        selected = await _select_sensitive_pages(
            match_pages,
            question,
            latest_judgement,
            limit=10,
            infra=infra,
        )
        selected_ids = {p.id for p in selected}
        pages_with_matches = [(p, cs) for p, cs in pages_with_matches if p.id in selected_ids][:10]

    async def _check_replacements(
        page: Page,
        candidates: Sequence[tuple[Page, float]],
    ) -> set[str]:
        async with _B2_SEMAPHORE:
            target_text = await format_page(
                page,
                PageDetail.CONTENT,
                linked_detail=PageDetail.ABSTRACT,
                db=infra.db,
            )
            candidate_parts: list[str] = []
            for c, _ in candidates:
                candidate_parts.append(
                    await format_page(
                        c,
                        PageDetail.CONTENT,
                        linked_detail=PageDetail.ABSTRACT,
                        db=infra.db,
                    )
                )
            candidate_descriptions = "\n\n---\n\n".join(candidate_parts)
            meta = LLMExchangeMetadata(
                call_id=infra.call.id,
                phase=f"big_assess_b2_replace_{page.id[:8]}",
            )
            result = await structured_call(
                "You are evaluating whether any candidate pages should replace a "
                "target page because they analyse the same subject matter at "
                "higher quality — better evidence, more nuance, stronger sourcing, "
                "or higher credibility. A candidate must be about the same topic "
                "as the target; do NOT select pages that are merely related to or "
                "adjacent to the target's subject. You may pick multiple candidates "
                "or none. Return the 8-char short IDs of qualifying candidates, "
                "or an empty list if none qualify.",
                user_message=(
                    f"## Target page\n\n{target_text}\n\n## Candidates\n\n{candidate_descriptions}"
                ),
                response_model=_ReplacementPick,
                metadata=meta,
                db=infra.db,
            )
        found: set[str] = set()
        if result.parsed:
            candidate_id_map = {c.id[:8]: c.id for c, _ in candidates}
            for short_id in result.parsed.replacement_ids:
                full_id = candidate_id_map.get(short_id)
                if full_id:
                    found.add(full_id)
        return found

    per_page_results = await asyncio.gather(
        *[_check_replacements(p, cs) for p, cs in pages_with_matches]
    )
    replacement_ids: set[str] = set()
    for ids in per_page_results:
        replacement_ids |= ids

    log.info("Find replacements: found %d replacement pages", len(replacement_ids))
    return replacement_ids


class _AbstractBatch(BaseModel):
    summaries: list[PageSummaryItem]


async def _generate_missing_abstracts(
    connected: Sequence[tuple[Page, PageLink]],
    infra: CallInfra,
) -> None:
    """Generate and persist abstracts for connected pages that lack them."""
    missing = [p for p, _ in connected if not p.abstract]
    if not missing:
        log.info("All connected pages have abstracts")
        return

    log.info(
        "Generating abstracts for %d connected pages: %s",
        len(missing),
        ", ".join(p.id[:8] for p in missing),
    )

    page_contents = "\n\n---\n\n".join(
        f"Page `{p.id[:8]}` — {p.headline}\n\n{p.content}" for p in missing
    )
    system_prompt = (
        "You are generating abstracts for workspace pages. "
        "You will be given page contents and must produce a self-contained "
        "abstract for each.\n\n"
        f"Page contents:\n\n{page_contents}"
    )
    page_lines = "\n".join(f'- `{p.id[:8]}`: "{p.headline[:120]}"' for p in missing)
    user_message = (
        "Generate an abstract for each of the following pages.\n\n"
        f"{page_lines}\n\n"
        f"Abstract requirements: {ABSTRACT_INSTRUCTION}\n\n"
        "For each page, return its page_id and abstract."
    )

    meta = LLMExchangeMetadata(
        call_id=infra.call.id,
        phase="big_assess_abstract_generation",
    )
    result = await structured_call(
        system_prompt,
        user_message=user_message,
        response_model=_AbstractBatch,
        metadata=meta,
        db=infra.db,
    )
    if result.parsed:
        await save_page_abstracts(result.parsed.summaries, infra.db)
        log.info(
            "Generated %d abstracts for connected pages",
            len(result.parsed.summaries),
        )


class BigAssessContext(ContextBuilder):
    """Elaborate context builder that freshens connected pages before assessment.

    Steps:
    1. Resolve superseded connections (swap links in DB)
    2. Reassess pages that cite superseded content
    3. Search for higher-quality replacement pages via embeddings
    4. Generate missing abstracts
    5. Build final embedding context with all gathered pages
    """

    def __init__(self, call_type: CallType) -> None:
        self._call_type = call_type

    async def build_context(self, infra: CallInfra) -> ContextResult:
        db = infra.db
        question = await db.get_page(infra.question_id)
        if not question:
            return ContextResult(
                context_text=f"[Question page {infra.question_id} not found]",
                working_page_ids=[],
            )

        latest_judgement = await _get_latest_judgement(infra.question_id, db)

        connected = await _resolve_superseded_connections(
            infra.question_id,
            latest_judgement,
            db,
        )
        log.info(
            "Big assess: %d connected pages after supersession resolution",
            len(connected),
        )

        await _reassess_pages_citing_superseded(
            connected,
            question,
            latest_judgement,
            infra,
        )
        latest_judgement = await _get_latest_judgement(infra.question_id, db)
        connected = await _resolve_superseded_connections(
            infra.question_id,
            latest_judgement,
            db,
        )
        log.info(
            "Big assess: %d connected pages after reassessment refresh",
            len(connected),
        )

        replacement_ids = await _find_higher_quality_replacements(
            connected,
            question,
            latest_judgement,
            infra,
        )

        await _generate_missing_abstracts(connected, infra)

        exclude_ids: set[str] = set()
        if latest_judgement:
            exclude_ids.add(latest_judgement.id)

        query = question.headline
        settings = get_settings()
        result = await build_embedding_based_context(
            query,
            db,
            scope_question_id=infra.question_id,
            scope_detail=PageDetail.CONTENT,
            scope_linked_detail=PageDetail.ABSTRACT,
            require_judgement_for_questions=True,
            full_page_char_budget=settings.big_assess_full_page_char_budget,
            abstract_page_char_budget=settings.big_assess_abstract_page_char_budget,
            summary_page_char_budget=settings.big_assess_summary_page_char_budget,
            full_page_similarity_floor=settings.big_assess_full_page_similarity_floor,
            abstract_page_similarity_floor=settings.big_assess_abstract_page_similarity_floor,
            exclude_page_ids=exclude_ids,
        )
        working_page_ids = result.page_ids

        judgement_cited_ids: list[str] = []
        if latest_judgement and latest_judgement.content:
            cited_sids = sorted(set(_CITATION_RE.findall(latest_judgement.content)))
            if cited_sids:
                resp = await db._execute(
                    db.client.table("pages")
                    .select("id,is_superseded")
                    .or_(",".join(f"id.like.{sid}%" for sid in cited_sids))
                )
                rows = resp.data or []
                prefix_to_row: dict[str, dict] = {}
                for row in rows:
                    prefix = row["id"][:8]
                    if prefix in prefix_to_row:
                        prefix_to_row.pop(prefix)
                    else:
                        prefix_to_row[prefix] = row

                superseded_ids = [
                    row["id"] for row in prefix_to_row.values() if row["is_superseded"]
                ]
                resolved = await db.resolve_supersession_chains(superseded_ids)

                for sid in cited_sids:
                    row = prefix_to_row.get(sid)
                    if not row:
                        continue
                    full_id: str = row["id"]
                    if full_id in exclude_ids:
                        continue
                    if row["is_superseded"]:
                        replacement = resolved.get(full_id)
                        if replacement and replacement.id not in exclude_ids:
                            judgement_cited_ids.append(replacement.id)
                    else:
                        judgement_cited_ids.append(full_id)

        extra_ids = judgement_cited_ids + list(replacement_ids)
        preloaded_ids = infra.call.context_page_ids or []
        all_extra = extra_ids + preloaded_ids

        context_text = result.context_text
        already_in_context = set(working_page_ids) | {infra.question_id} | exclude_ids
        if all_extra:
            extra_pages = await db.get_pages_by_ids(all_extra)
            parts: list[str] = []
            for pid in all_extra:
                if pid in already_in_context:
                    continue
                already_in_context.add(pid)
                page = extra_pages.get(pid)
                if page and page.is_active():
                    parts += [
                        "",
                        "---",
                        "",
                        f"## Pre-loaded Page: `{pid[:8]}`",
                        "",
                        await format_page(
                            page,
                            PageDetail.CONTENT,
                            db=db,
                            track=True,
                            track_tags={"source": "big_assess_extra"},
                        ),
                    ]
            if parts:
                context_text += "\n".join(parts)

        await _record_context_built(infra, working_page_ids, all_extra)
        return ContextResult(
            context_text=context_text,
            working_page_ids=working_page_ids,
            preloaded_ids=all_extra,
        )


class SpecOnlyContext(ContextBuilder):
    """Context containing ONLY the spec items for an artefact-task question.

    Used by generate_artefact: the generator must produce the artefact from
    the spec alone, with no access to the broader workspace. Any information
    the artefact should reflect must be captured in a spec item.
    """

    async def build_context(self, infra: CallInfra) -> ContextResult:
        task = await infra.db.get_page(infra.question_id)
        if task is None:
            raise ValueError(
                f"SpecOnlyContext: artefact-task question {infra.question_id} not found"
            )

        spec_pages = await active_spec_items_for_task(infra.question_id, infra.db)

        parts: list[str] = [
            "# Artefact task",
            "",
            task.headline,
            "",
            task.content or "(no further description)",
            "",
            "---",
            "",
            f"# Spec ({len(spec_pages)} items)",
            "",
        ]
        if not spec_pages:
            parts.append("(no spec items yet)")
        else:
            for i, spec in enumerate(spec_pages, start=1):
                parts += [
                    f"## {i}. {spec.headline}",
                    "",
                    spec.content,
                    "",
                ]
        context_text = "\n".join(parts)

        working_page_ids = [task.id] + [p.id for p in spec_pages]
        await _record_context_built(infra, working_page_ids, [])
        return ContextResult(
            context_text=context_text,
            working_page_ids=working_page_ids,
            preloaded_ids=[],
        )


class CritiqueContext(ContextBuilder):
    """Context for critiquing the latest artefact on an artefact-task question.

    Includes the artefact-task question, the latest active Artefact produced
    for it, the prior request-only critique of that artefact (when one
    exists), and an embedding-based sweep over the broader workspace. Spec
    items are deliberately EXCLUDED — the critic judges the artefact against
    the request and what the workspace knows, not against the spec. That is
    how spec-gaps get surfaced.

    The request-only critique runs first; this critic sees its output and
    is asked to add what the request-only critic couldn't see (workspace
    grounding). That ordering keeps the two critiques complementary
    rather than redundant.
    """

    async def build_context(self, infra: CallInfra) -> ContextResult:
        task = await infra.db.get_page(infra.question_id)
        if task is None:
            raise ValueError(
                f"CritiqueContext: artefact-task question {infra.question_id} not found"
            )

        artefact = await infra.db.latest_artefact_for_task(infra.question_id)
        if artefact is None:
            raise ValueError(
                f"CritiqueContext: no artefact found for task {infra.question_id}; "
                "run generate_artefact first."
            )

        prior = await _latest_request_only_critique(artefact.id, infra.db)
        prior_section = _format_prior_request_only_critique(prior)

        query = task.headline or task.content[:200]
        embedding_result = await build_embedding_based_context(
            query,
            infra.db,
            scope_question_id=infra.question_id,
        )

        parts: list[str] = [
            "# Artefact task",
            "",
            task.headline,
            "",
            task.content or "(no further description)",
            "",
            "---",
            "",
            "# Artefact under review",
            "",
            f"**{artefact.headline}**",
            "",
            artefact.content,
            "",
            "---",
            "",
            *prior_section,
            "# Broader workspace context",
            "",
            embedding_result.context_text,
        ]
        context_text = "\n".join(parts)

        working_page_ids = [task.id, artefact.id, *embedding_result.page_ids]
        if prior is not None:
            working_page_ids.append(prior.id)
        await _record_context_built(infra, working_page_ids, [])
        return ContextResult(
            context_text=context_text,
            working_page_ids=working_page_ids,
            preloaded_ids=[],
        )


async def _latest_request_only_critique(artefact_id: str, db: DB) -> Page | None:
    """Return the most recent active request-only critique of *artefact_id*."""
    inbound = await db.get_links_to(artefact_id)
    candidate_ids = [l.from_page_id for l in inbound if l.link_type == LinkType.CRITIQUE_OF]
    if not candidate_ids:
        return None
    pages_by_id = await db.get_pages_by_ids(candidate_ids)
    candidates = [
        p
        for p in pages_by_id.values()
        if p.is_active()
        and p.page_type == PageType.JUDGEMENT
        and p.provenance_call_type == CallType.CRITIQUE_ARTEFACT_REQUEST_ONLY.value
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p.created_at, p.id))


def _format_prior_request_only_critique(prior: Page | None) -> list[str]:
    """Return the section that renders a prior request-only critique, or empty."""
    if prior is None:
        return []
    grade = prior.extra.get("grade")
    issues = prior.extra.get("issues") or []
    overall = ""
    # Best-effort: pull the "Overall" line out of the rendered content if present.
    for line in prior.content.splitlines():
        marker = "**Overall:**"
        if line.startswith(marker):
            overall = line[len(marker) :].strip()
            break
    parts = [
        "# Prior request-only critique (your job is to extend it, not repeat it)",
        "",
        "A first reviewer has already evaluated this artefact using only the "
        "task description and the artefact itself (no workspace context). Their "
        "findings are below. Your job is to add what they could not see — issues "
        "that depend on the broader workspace context — not to repeat or rephrase "
        "their points. If the workspace simply confirms one of their findings, you "
        "may briefly note that, but don't pad your output with redundant items.",
        "",
    ]
    if grade is not None:
        parts.append(f"**Grade (request-only):** {grade}/10")
    if overall:
        parts.append(f"**Overall:** {overall}")
    if issues:
        parts.append("")
        parts.append("**Issues already raised by the request-only critic:**")
        for issue in issues:
            parts.append(f"- {issue}")
    parts += ["", "---", ""]
    return parts


_CRITIQUE_KIND_LABELS = {
    CallType.CRITIQUE_ARTEFACT.value: "Workspace-aware critique",
    CallType.CRITIQUE_ARTEFACT_REQUEST_ONLY.value: "Request-only critique",
}


def _append_critique_section(parts: list[str], kind: str, critique: Page) -> None:
    """Render one critique block in the RefinementContext output."""
    label = _CRITIQUE_KIND_LABELS.get(kind, "Critique")
    grade = critique.extra.get("grade")
    issues = critique.extra.get("issues") or []
    parts.append(f"### {label}")
    parts.append("")
    if grade is not None:
        parts.append(f"**Grade:** {grade}/10")
    if issues:
        parts.append("")
        parts.append("**Issues:**")
        for issue in issues:
            parts.append(f"- {issue}")
    parts.append("")


async def active_spec_items_for_task(task_id: str, db) -> list[Page]:
    """Return active SPEC_ITEM pages SPEC_OF-linked to *task_id*.

    Sorted by ``created_at`` (stable insertion order) so prompt rendering is
    deterministic across runs — important for prompt caching and for keeping
    generated artefacts consistent when the spec hasn't actually changed.
    """
    links = await db.get_links_to(task_id)
    spec_of_links = [l for l in links if l.link_type == LinkType.SPEC_OF]
    if not spec_of_links:
        return []
    pages_by_id = await db.get_pages_by_ids([l.from_page_id for l in spec_of_links])
    active = [
        p for p in pages_by_id.values() if p.is_active() and p.page_type == PageType.SPEC_ITEM
    ]
    active.sort(key=lambda p: (p.created_at, p.id))
    return active


class RequestOnlyCritiqueContext(ContextBuilder):
    """Critique context with no workspace exposure.

    Sees the artefact-task question and the latest artefact and nothing else.
    Used by the second-opinion request-only critic, which evaluates "does this
    answer what was asked?" as a fresh outside reader — uncontaminated by
    whatever the workspace happens to know about the topic. Pairs with the
    workspace-aware CritiqueContext so the refiner sees two complementary
    angles per iteration.
    """

    async def build_context(self, infra: CallInfra) -> ContextResult:
        task = await infra.db.get_page(infra.question_id)
        if task is None:
            raise ValueError(
                f"RequestOnlyCritiqueContext: artefact-task question {infra.question_id} not found"
            )

        artefact = await infra.db.latest_artefact_for_task(infra.question_id)
        if artefact is None:
            raise ValueError(
                f"RequestOnlyCritiqueContext: no artefact found for task "
                f"{infra.question_id}; run generate_artefact first."
            )

        parts: list[str] = [
            "# Artefact task",
            "",
            task.headline,
            "",
            task.content or "(no further description)",
            "",
            "---",
            "",
            "# Artefact under review",
            "",
            f"**{artefact.headline}**",
            "",
            artefact.content,
        ]
        context_text = "\n".join(parts)

        working_page_ids = [task.id, artefact.id]
        await _record_context_built(infra, working_page_ids, [])
        return ContextResult(
            context_text=context_text,
            working_page_ids=working_page_ids,
            preloaded_ids=[],
        )


class RefinementContext(ContextBuilder):
    """Context for the refine_spec call.

    Shows the refiner:
    1. The original artefact-task request.
    2. The current spec (same format as SpecOnlyContext).
    3. The last-3 iteration triples — for each of the three most recent
       artefact versions linked ARTEFACT_OF to the task: the spec items it
       was generated from (via GENERATED_FROM — captured at generation time
       so deleted spec items still appear in historical triples), the
       artefact itself, and its critique.

    Does NOT do a main-workspace embedding sweep — refinement operates on
    the closed feedback loop of spec → artefact → critique.
    """

    def __init__(self, window: int = 3) -> None:
        self._window = window

    async def build_context(self, infra: CallInfra) -> ContextResult:
        task = await infra.db.get_page(infra.question_id)
        if task is None:
            raise ValueError(
                f"RefinementContext: artefact-task question {infra.question_id} not found"
            )

        current_spec = await active_spec_items_for_task(infra.question_id, infra.db)

        links = await infra.db.get_links_to(infra.question_id)
        artefact_links = [l for l in links if l.link_type == LinkType.ARTEFACT_OF]
        artefact_ids = [l.from_page_id for l in artefact_links]
        artefacts_by_id = await infra.db.get_pages_by_ids(artefact_ids)
        artefacts = [p for p in artefacts_by_id.values() if p.page_type == PageType.ARTEFACT]
        artefacts.sort(key=lambda p: (p.created_at, p.id))
        recent = artefacts[-self._window :]

        triples: list[tuple[Page, list[Page], dict[str, Page]]] = []
        for artefact in recent:
            snapshot_links = await infra.db.get_links_from(artefact.id)
            spec_ids = [
                l.to_page_id for l in snapshot_links if l.link_type == LinkType.GENERATED_FROM
            ]
            snapshot_pages_by_id = await infra.db.get_pages_by_ids(spec_ids) if spec_ids else {}
            snapshot_specs = [
                snapshot_pages_by_id[pid] for pid in spec_ids if pid in snapshot_pages_by_id
            ]
            snapshot_specs.sort(key=lambda p: (p.created_at, p.id))

            inbound = await infra.db.get_links_to(artefact.id)
            critique_link_ids = [
                l.from_page_id for l in inbound if l.link_type == LinkType.CRITIQUE_OF
            ]
            critique_pages_by_id = (
                await infra.db.get_pages_by_ids(critique_link_ids) if critique_link_ids else {}
            )
            critiques_by_kind: dict[str, Page] = {}
            for p in critique_pages_by_id.values():
                if not p.is_active() or p.page_type != PageType.JUDGEMENT:
                    continue
                kind = p.provenance_call_type or ""
                existing = critiques_by_kind.get(kind)
                if existing is None or p.created_at > existing.created_at:
                    critiques_by_kind[kind] = p

            triples.append((artefact, snapshot_specs, critiques_by_kind))

        parts: list[str] = [
            "# Artefact task",
            "",
            task.headline,
            "",
            task.content or "(no further description)",
            "",
            "---",
            "",
            f"# Current spec ({len(current_spec)} items)",
            "",
        ]
        if current_spec:
            for i, spec in enumerate(current_spec, start=1):
                parts += [
                    f"## {i}. [{spec.id[:8]}] {spec.headline}",
                    "",
                    spec.content,
                    "",
                ]
        else:
            parts.append("(no spec items)")

        parts += ["", "---", "", f"# Last {len(triples)} iterations (oldest first)", ""]
        if not triples:
            parts.append("(no iterations yet — call regenerate_and_critique to start)")
        else:
            for iter_index, (artefact, snapshot_specs, critiques_by_kind) in enumerate(
                triples, start=1
            ):
                parts += [f"## Iteration {iter_index} — artefact [{artefact.id[:8]}]", ""]
                parts.append(f"### Spec used ({len(snapshot_specs)} items)")
                parts.append("")
                if snapshot_specs:
                    for spec in snapshot_specs:
                        parts.append(f"- [{spec.id[:8]}] **{spec.headline}** — {spec.content}")
                else:
                    parts.append("(none captured)")
                parts += ["", f"### Artefact: {artefact.headline}", "", artefact.content, ""]

                if not critiques_by_kind:
                    parts += ["### Critiques", "", "(no critiques recorded for this iteration)", ""]
                else:
                    # Render workspace-aware first, then request-only, then any others.
                    ordered_kinds = [
                        CallType.CRITIQUE_ARTEFACT.value,
                        CallType.CRITIQUE_ARTEFACT_REQUEST_ONLY.value,
                    ]
                    seen: set[str] = set()
                    for kind in ordered_kinds:
                        critique = critiques_by_kind.get(kind)
                        if critique is None:
                            continue
                        seen.add(kind)
                        _append_critique_section(parts, kind, critique)
                    for kind, critique in critiques_by_kind.items():
                        if kind in seen:
                            continue
                        _append_critique_section(parts, kind, critique)

                parts.append("---")
                parts.append("")

        context_text = "\n".join(parts)
        working_page_ids = [
            task.id,
            *[p.id for p in current_spec],
            *[a.id for a, _, _ in triples],
        ]
        await _record_context_built(infra, working_page_ids, [])
        return ContextResult(
            context_text=context_text,
            working_page_ids=working_page_ids,
            preloaded_ids=[],
        )
