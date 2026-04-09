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
    _format_loaded_pages,
    _run_phase1,
    resolve_page_refs,
    save_page_abstracts,
)
from rumil.calls.stages import CallInfra, ContextBuilder, ContextResult
from rumil.context import (
    build_call_context,
    build_embedding_based_context,
    build_scout_context,
    format_page,
)
from rumil.database import DB
from rumil.embeddings import search_pages
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
    FindConsiderationsMode,
)
from rumil.settings import get_settings
from rumil.page_graph import PageGraph, SubtreeGraph
from rumil.tracing.trace_events import ContextBuiltEvent
from rumil.workspace_map import build_workspace_map

log = logging.getLogger(__name__)

_B2_SEMAPHORE = asyncio.Semaphore(15)


async def _record_context_built(
    infra: CallInfra,
    working_page_ids: list[str],
    preloaded_ids: list[str],
    *,
    source_page_id: str | None = None,
    scout_mode: str | None = None,
) -> None:
    await infra.trace.record(
        ContextBuiltEvent(
            working_context_page_ids=await resolve_page_refs(
                working_page_ids, infra.db
            ),
            preloaded_page_ids=await resolve_page_refs(preloaded_ids, infra.db),
            source_page_id=source_page_id,
            scout_mode=scout_mode,
        )
    )


async def _do_phase1(
    infra: CallInfra,
    call_type: CallType,
    context_text: str,
) -> tuple[str, list[str]]:
    """Run phase1 page loading and return (updated context_text, phase1_ids)."""
    system_prompt = build_system_prompt(call_type.value)
    phase1_ids = await _run_phase1(
        system_prompt,
        context_text,
        infra.call.id,
        infra.state,
        infra.db,
    )
    if phase1_ids:
        extra_text = await _format_loaded_pages(phase1_ids, infra.db)
        context_text += "\n\n## Loaded Pages\n\n" + extra_text
    return context_text, phase1_ids


class EmbeddingContext(ContextBuilder):
    """Embedding-based context (no phase1). Used by embedding variants."""

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
                        await format_page(page, PageDetail.CONTENT, db=infra.db),
                    ]
            context_text += "\n".join(parts)

        await _record_context_built(infra, working_page_ids, preloaded_ids)
        return ContextResult(
            context_text=context_text,
            working_page_ids=working_page_ids,
            preloaded_ids=preloaded_ids,
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

        source_section = (
            "\n\n---\n\n## Source Document\n\n"
            f"**File:** {self._filename}  \n"
            f"**Source page ID:** `{self._source_page.id}`\n\n"
            f"{self._source_page.content}"
        )
        return ContextResult(
            context_text=result.context_text + source_section,
            working_page_ids=working_page_ids,
        )


class ScoutEmbeddingContext(ContextBuilder):
    """Scout context built via embedding similarity search."""

    def __init__(self, mode: FindConsiderationsMode) -> None:
        self._mode = mode

    async def build_context(self, infra: CallInfra) -> ContextResult:
        graph = await SubtreeGraph.load_for_root(
            infra.db,
            infra.question_id,
            include_ancestors=True,
            include_ancestor_children=True,
        )
        scout_ctx = await build_scout_context(
            infra.question_id,
            infra.db,
            graph=graph,
        )
        working_page_ids = scout_ctx.page_ids

        await infra.trace.record(
            ContextBuiltEvent(
                working_context_page_ids=await resolve_page_refs(
                    working_page_ids,
                    infra.db,
                ),
                preloaded_page_ids=[],
                scout_mode=self._mode.value,
            )
        )

        return ContextResult(
            context_text=scout_ctx.context_text,
            working_page_ids=working_page_ids,
        )


class ConceptScoutContext(ContextBuilder):
    """Graph context + concept registry + phase1. Used by ScoutConceptsCall."""

    def __init__(self, call_type: CallType) -> None:
        self._call_type = call_type

    async def build_context(self, infra: CallInfra) -> ContextResult:
        preloaded_ids = infra.call.context_page_ids or []
        graph = await PageGraph.load(infra.db)
        context_text, _, working_page_ids = await build_call_context(
            infra.question_id,
            infra.db,
            extra_page_ids=preloaded_ids,
            graph=graph,
        )

        registry = await infra.db.get_concept_registry()
        if registry:
            lines = ["## Concept Registry", ""]
            lines.append(
                "The following concepts have already been proposed (do not re-propose them):"
            )
            lines.append("")
            for concept in registry:
                extra = concept.extra or {}
                stage = extra.get("stage", "proposed")
                score = extra.get("score")
                promoted = extra.get("promoted", False)
                status = (
                    "promoted"
                    if promoted
                    else (f"score={score}" if score is not None else stage)
                )
                lines.append(f"- [{status}] `{concept.id[:8]}` — {concept.headline}")
            context_text += "\n\n" + "\n".join(lines)

        await _record_context_built(infra, working_page_ids, preloaded_ids)
        context_text, phase1_ids = await _do_phase1(
            infra,
            self._call_type,
            context_text,
        )
        return ContextResult(
            context_text=context_text,
            working_page_ids=working_page_ids,
            preloaded_ids=preloaded_ids,
            phase1_ids=phase1_ids,
        )


class ConceptAssessContext(ContextBuilder):
    """Workspace map + concept page + assessment history + phase1.
    Used by AssessConceptCall.
    """

    def __init__(self, phase: str) -> None:
        self._phase = phase

    async def build_context(self, infra: CallInfra) -> ContextResult:
        concept = await infra.db.get_page(infra.question_id)
        if not concept:
            return ContextResult(
                context_text=f"[Concept page {infra.question_id} not found]",
                working_page_ids=[],
            )

        map_text, _ = await build_workspace_map(infra.db)
        concept_text = await format_page(concept, PageDetail.HEADLINE, db=infra.db)

        extra = concept.extra or {}
        assessment_rounds = extra.get("assessment_rounds", [])
        history_section = ""
        if assessment_rounds:
            lines = ["## Previous Assessment Rounds", ""]
            for i, r in enumerate(assessment_rounds):
                lines.append(
                    f"Round {i + 1} ({r.get('phase', '?')}): "
                    f"score={r.get('score', '?')}, "
                    f"remaining_fruit={r.get('remaining_fruit', '?')}"
                )
                if r.get("what_worked"):
                    lines.append(f"  Worked: {r['what_worked']}")
                if r.get("what_didnt"):
                    lines.append(f"  Didn't: {r['what_didnt']}")
            history_section = "\n\n" + "\n".join(lines)

        context_text = (
            "\n\n".join(
                [
                    map_text,
                    "---",
                    "## Concept Under Assessment",
                    "",
                    concept_text,
                ]
            )
            + history_section
        )

        working_page_ids = [infra.question_id]
        preloaded_ids = infra.call.context_page_ids or []
        await infra.trace.record(
            ContextBuiltEvent(
                working_context_page_ids=await resolve_page_refs(
                    working_page_ids,
                    infra.db,
                ),
                preloaded_page_ids=await resolve_page_refs(preloaded_ids, infra.db),
            )
        )
        context_text, phase1_ids = await _do_phase1(
            infra,
            CallType.ASSESS_CONCEPT,
            context_text,
        )
        return ContextResult(
            context_text=context_text,
            working_page_ids=working_page_ids,
            preloaded_ids=preloaded_ids,
            phase1_ids=phase1_ids,
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
    page_ids: list[str] = Field(
        description="8-char short IDs of the selected pages"
    )


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
            cite_pattern = re.compile(
                rf'[^\n]*\[{re.escape(p.id[:8])}\][^\n]*'
            )
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
            f"## Judgement\n\n"
            f"**{latest_judgement.headline}**\n\n"
            f"{latest_judgement.content}"
        )
    user_message_parts.append(
        f"## Candidate pages\n\n"
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
    LinkType.DEPENDS_ON,
}


async def _gather_connected_pages(
    page_id: str, db: DB,
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
                link.id[:8], new_from[:8], new_to[:8],
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
    question_id: str, db: DB,
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

    refreshed: list[tuple[Page, PageLink]] = []
    for page, link in connected:
        if page.is_superseded:
            new_page = await db.resolve_supersession_chain(page.id)
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
    full_ids: list[str] = []
    for sid in short_ids:
        resolved = await db.resolve_page_id(sid)
        if resolved:
            full_ids.append(resolved)
    if not full_ids:
        return False
    cited_pages = await db.get_pages_by_ids(full_ids)
    return any(p.is_superseded for p in cited_pages.values())


async def _reassess_pages_citing_superseded(
    connected: list[tuple[Page, PageLink]],
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
            stale, question, latest_judgement, limit=5, infra=infra,
        )

    async def _do_reassess(page: Page) -> None:
        from rumil.clean.common import reassess_claim, reassess_question

        if page.page_type == PageType.QUESTION:
            await reassess_question(
                page.id, [], infra.call, infra.db, infra.trace,
                assess_variant="default",
            )
        elif page.page_type == PageType.JUDGEMENT:
            links = await infra.db.get_links_from(page.id)
            question_links = [
                l for l in links if l.link_type == LinkType.RELATED
            ]
            if question_links:
                question_id = question_links[0].to_page_id
                log.info(
                    "Stale judgement %s: reassessing its question %s",
                    page.id[:8], question_id[:8],
                )
                await reassess_question(
                    question_id, [], infra.call, infra.db, infra.trace,
                    assess_variant="default",
                )
            else:
                log.warning(
                    "Stale judgement %s has no linked question, skipping",
                    page.id[:8],
                )
        else:
            await reassess_claim(
                page.id, "", infra.call, infra.db, infra.trace,
            )

    log.info("Reassess stale: reassessing %d pages", len(stale))
    await asyncio.gather(*[_do_reassess(p) for p in stale])


async def _find_higher_quality_replacements(
    connected: list[tuple[Page, PageLink]],
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
            query_text = page.abstract if page.abstract else page.headline
            results = await search_pages(
                infra.db, query_text, match_threshold=0.6, match_count=5,
            )
        filtered = [
            (p, score) for p, score in results
            if p.id not in exclude_ids and p.is_active()
        ]
        return page, filtered

    search_results = await asyncio.gather(
        *[_search_for_page(page) for page, _ in connected]
    )

    for page, candidates in search_results:
        if candidates:
            match_titles = ", ".join(
                f"{c.headline[:40]} ({score:.2f})" for c, score in candidates
            )
            log.info(
                "Find replacements: %s (%s) — %d matches: %s",
                page.id[:8], page.headline[:50], len(candidates), match_titles,
            )
        else:
            log.info(
                "Find replacements: %s (%s) — no matches",
                page.id[:8], page.headline[:50],
            )

    pages_with_matches = [
        (page, candidates) for page, candidates in search_results if candidates
    ]

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
            match_pages, question, latest_judgement, limit=10, infra=infra,
        )
        selected_ids = {p.id for p in selected}
        pages_with_matches = [
            (p, cs) for p, cs in pages_with_matches
            if p.id in selected_ids
        ][:10]

    async def _check_replacements(
        page: Page, candidates: list[tuple[Page, float]],
    ) -> set[str]:
        async with _B2_SEMAPHORE:
            target_text = await format_page(
                page, PageDetail.CONTENT, linked_detail=PageDetail.ABSTRACT,
                db=infra.db,
            )
            candidate_parts: list[str] = []
            for c, _ in candidates:
                candidate_parts.append(
                    await format_page(
                        c, PageDetail.CONTENT, linked_detail=PageDetail.ABSTRACT,
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
                    f"## Target page\n\n{target_text}\n\n"
                    f"## Candidates\n\n{candidate_descriptions}"
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
        f'Page `{p.id[:8]}` — {p.headline}\n\n{p.content}'
        for p in missing
    )
    system_prompt = (
        "You are generating abstracts for workspace pages. "
        "You will be given page contents and must produce a self-contained "
        "abstract for each.\n\n"
        f"Page contents:\n\n{page_contents}"
    )
    page_lines = "\n".join(
        f'- `{p.id[:8]}`: "{p.headline[:120]}"'
        for p in missing
    )
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
            infra.question_id, latest_judgement, db,
        )
        log.info(
            "Big assess: %d connected pages after supersession resolution",
            len(connected),
        )

        await _reassess_pages_citing_superseded(
            connected, question, latest_judgement, infra,
        )
        latest_judgement = await _get_latest_judgement(infra.question_id, db)
        connected = await _resolve_superseded_connections(
            infra.question_id, latest_judgement, db,
        )
        log.info(
            "Big assess: %d connected pages after reassessment refresh",
            len(connected),
        )

        replacement_ids = await _find_higher_quality_replacements(
            connected, question, latest_judgement, infra,
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
            cited_sids = sorted(set(
                _CITATION_RE.findall(latest_judgement.content)
            ))
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
                    row["id"] for row in prefix_to_row.values()
                    if row["is_superseded"]
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
            parts: list[str] = []
            for pid in all_extra:
                if pid in already_in_context:
                    continue
                already_in_context.add(pid)
                page = await db.get_page(pid)
                if page and page.is_active():
                    parts += [
                        "",
                        "---",
                        "",
                        f"## Pre-loaded Page: `{pid[:8]}`",
                        "",
                        await format_page(page, PageDetail.CONTENT, db=db),
                    ]
            if parts:
                context_text += "\n".join(parts)

        await _record_context_built(infra, working_page_ids, all_extra)
        return ContextResult(
            context_text=context_text,
            working_page_ids=working_page_ids,
            preloaded_ids=all_extra,
        )
