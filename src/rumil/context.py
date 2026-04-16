"""
Build context text from workspace pages for injection into LLM prompts.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from rumil.database import DB
from rumil.embeddings import embed_query, search_pages_by_vector
from rumil.models import Page, PageDetail, PageLink, PageType
from rumil.settings import get_settings

log = logging.getLogger(__name__)

CREDENCE_LABELS: dict[int, str] = {
    9: "Completely uncontroversial (>99.99%)",
    8: "Almost certain (99–99.99%)",
    7: "Very likely (90–99%)",
    6: "Likely (70–90%)",
    5: "Genuinely uncertain (30–70%)",
    4: "Plausible but doubtful (10–30%)",
    3: "Unlikely (1–10%)",
    2: "Extremely unlikely (0.01–1%)",
    1: "Virtually impossible (<0.01%)",
}


def group_by_credence(
    items: Sequence[tuple[str, Page]],
    heading_level: str = "###",
    separator: str = "\n\n",
) -> str:
    """Group pre-formatted page strings by descending credence.

    Items with credence=None (questions) go in a "Questions" group first.
    Empty groups are skipped.
    """
    questions: list[str] = []
    by_credence: dict[int, list[str]] = {}
    for text, page in items:
        if page.credence is None:
            questions.append(text)
        else:
            by_credence.setdefault(page.credence, []).append(text)

    parts: list[str] = []
    if questions:
        parts.append(f"{heading_level} Questions")
        parts.append(separator.join(questions))
    for c in range(9, 0, -1):
        if c in by_credence:
            parts.append(f"{heading_level} Credence {c} — {CREDENCE_LABELS[c]}")
            parts.append(separator.join(by_credence[c]))
    return "\n\n".join(parts)


@dataclass
class EmbeddingBasedContextResult:
    context_text: str
    page_ids: list[str]
    full_page_ids: list[str]
    abstract_page_ids: list[str]
    summary_page_ids: list[str]
    distillation_page_ids: list[str] = field(default_factory=list)
    budget_usage: dict[str, int] = field(default_factory=dict)


async def render_page_and_immediate_children(
    root_id: str,
    db: DB,
    *,
    detail: PageDetail = PageDetail.CONTENT,
    linked_detail: PageDetail | None = PageDetail.HEADLINE,
    content_page_ids: set[str] | None = None,
) -> str:
    """Render a page and its direct child questions (one level deep).

    For question pages: renders the root with its considerations and
    judgements, then each direct child question with their considerations
    and judgements. Non-question root pages are rendered standalone.

    Pages whose IDs appear in *content_page_ids* are rendered at CONTENT
    detail regardless of the *detail* parameter.

    Uses batched DB queries: O(1) round trips regardless of child count.
    """
    root = await db.get_page(root_id)
    if not root:
        return f"[Page {root_id} not found]"

    if root.page_type != PageType.QUESTION:
        return await format_page(root, detail, linked_detail=linked_detail, db=db)

    _content_ids = content_page_ids or set()

    children = await db.get_child_questions(root_id)
    all_question_ids = [root_id] + [c.id for c in children]
    considerations_by_q, judgements_by_q = (
        await db.get_considerations_for_questions(all_question_ids),
        await db.get_judgements_for_questions(all_question_ids),
    )

    parts: list[str] = []
    visited: set[str] = set()

    async def _render_question(question: Page, depth: int) -> None:
        if question.id in visited:
            parts.append(f"{'  ' * depth}(cycle: `{question.id[:8]}`)")
            return
        visited.add(question.id)

        q_detail = PageDetail.CONTENT if question.id in _content_ids else detail
        indent = "  " * depth
        parts.append(
            indent + await format_page(question, q_detail, linked_detail=None, db=db)
        )

        con_items: list[tuple[str, Page]] = []
        for claim, link in considerations_by_q.get(question.id, []):
            visited.add(claim.id)
            direction = f"({link.direction.value}) " if link.direction else ""
            line = f"{indent}- {direction}" + await format_page(
                claim, linked_detail or PageDetail.HEADLINE, linked_detail=None, db=db
            )
            if link.reasoning:
                line += f"\n{indent}  Reasoning: {link.reasoning}"
            con_items.append((line, claim))
        if con_items:
            parts.append("")
            hn = min(depth + 3, 6)
            grouped = group_by_credence(
                con_items, heading_level="#" * hn, separator="\n"
            )
            parts.append(grouped)

        all_judgements = judgements_by_q.get(question.id, [])
        judgements = (
            [max(all_judgements, key=lambda j: j.created_at)] if all_judgements else []
        )
        if judgements:
            parts.append("")
            parts.append(f"{indent}**Judgements:**")
            for j in judgements:
                visited.add(j.id)
                parts.append(
                    f"{indent}- "
                    + await format_page(
                        j,
                        linked_detail or PageDetail.HEADLINE,
                        linked_detail=None,
                        db=db,
                    )
                )

    await _render_question(root, 0)

    if children:
        parts.append("")
        parts.append("**Sub-questions:**")
        parts.append("")
        for child in children:
            await _render_question(child, 1)
            parts.append("")

    return "\n".join(parts)


async def _resolve_superseding_page(
    page: Page,
    db: DB | None,
) -> Page | None:
    """Resolve the supersession chain to the final active replacement page."""
    if db is not None:
        return await db.resolve_supersession_chain(page.id)
    return None


_CITATION_RE = re.compile(r"\[([a-f0-9]{8})\]")


async def _supersession_notes(body: str, db: DB) -> str:
    """Return a note block for any inline citations that reference superseded pages.

    Typically 2 DB queries: one to find superseded cited pages (prefix-match +
    ``is_superseded`` filter), one bulk chain resolution.
    """
    short_ids = sorted(set(_CITATION_RE.findall(body)))
    if not short_ids:
        return ""

    resp = await db._execute(
        db.client.table("pages")
        .select("id")
        .eq("is_superseded", True)
        .or_(",".join(f"id.like.{sid}%" for sid in short_ids))
    )
    rows = resp.data or []
    if not rows:
        return ""

    prefix_to_full: dict[str, str] = {}
    for row in rows:
        prefix = row["id"][:8]
        if prefix in prefix_to_full:
            prefix_to_full.pop(prefix)
        else:
            prefix_to_full[prefix] = row["id"]

    if not prefix_to_full:
        return ""

    superseded_ids = list(prefix_to_full.values())
    resolved = await db.resolve_supersession_chains(superseded_ids)

    notes: list[str] = []
    for sid in short_ids:
        full_id = prefix_to_full.get(sid)
        if not full_id:
            continue
        replacement = resolved.get(full_id)
        if not replacement:
            notes.append(
                f"> **Note:** `[{sid}]` has been superseded (replacement not found)."
            )
            continue
        abstract_text = replacement.abstract or replacement.headline
        notes.append(
            f"> **Note:** `[{sid}]` has been superseded by "
            f"`[{replacement.id[:8]}]` — {replacement.headline}\n"
            f"> {abstract_text}"
        )

    if not notes:
        return ""
    return "\n\n" + "\n\n".join(notes)


async def format_page(
    page: Page,
    detail: PageDetail = PageDetail.CONTENT,
    *,
    linked_detail: PageDetail | None = PageDetail.HEADLINE,
    db: DB | None = None,
    include_superseding: bool = True,
    exclude_page_ids: set[str] | None = None,
    highlight_run_id: str | None = None,
) -> str:
    """Format a single page at the requested detail level.

    - HEADLINE: one-liner with type, epistemic status, short ID, and headline.
    - ABSTRACT: header block + abstract text.
    - CONTENT: header block + full content.

    *linked_detail* controls how considerations, judgements, and sub-question
    judgements are rendered for question pages. Set to None to omit them
    entirely.

    When *include_superseding* is True (the default) and the page is
    superseded, the output includes the superseded page annotated as such,
    followed by the final replacement page rendered at the same detail level.
    """
    if include_superseding and page.is_superseded:
        replacement = await _resolve_superseding_page(page, db)
        original = await format_page(
            page,
            detail,
            linked_detail=linked_detail,
            db=db,
            include_superseding=False,
            highlight_run_id=highlight_run_id,
        )
        if replacement:
            replacement_text = await format_page(
                replacement,
                detail,
                linked_detail=linked_detail,
                db=db,
                include_superseding=False,
                highlight_run_id=highlight_run_id,
            )
            if detail == PageDetail.HEADLINE:
                return f"[SUPERSEDED] {original}\n  -> replaced by: {replacement_text}"
            return (
                f"{original}\n\n"
                "> **SUPERSEDED** — this page has been replaced by"
                f" `{replacement.id[:8]}` ({replacement.headline})."
                " Current version:\n\n"
                f"{replacement_text}"
            )
        if detail == PageDetail.HEADLINE:
            return f"[SUPERSEDED] {original}"
        return (
            f"{original}\n\n"
            "> **SUPERSEDED** — this page has been replaced"
            " (replacement not found)."
        )

    if detail != PageDetail.HEADLINE and not page.content and db:
        full = await db.get_page(page.id)
        if full:
            page = full

    _is_highlighted = (
        highlight_run_id and page.run_id and page.run_id == highlight_run_id
    )

    if detail == PageDetail.HEADLINE:
        tag = f"{page.page_type.value.upper()}"
        if page.credence is not None:
            tag += f" C{page.credence}/R{page.robustness}"
        prefix = "[ADDED BY THIS RUN] " if _is_highlighted else ""
        return f"{prefix}[{tag}] `{page.id[:8]}` -- {page.headline}"

    extra = page.extra or {}
    lines = [
        f"### [{page.page_type.value.upper()}] {page.headline}",
        f"ID: {page.id}",
    ]
    if _is_highlighted:
        lines.append("**[ADDED BY THIS RUN]**")
    if page.credence is not None:
        lines.append(f"Credence: {page.credence}/9 | Robustness: {page.robustness}/5")
    for k, v in extra.items():
        lines.append(f"{k}: {v}")

    body = page.abstract if detail == PageDetail.ABSTRACT else page.content
    if body:
        lines += ["", body]
        if db:
            notes = await _supersession_notes(body, db)
            if notes:
                lines.append(notes)

    _exclude = exclude_page_ids or set()
    if linked_detail is not None and db and page.page_type == PageType.QUESTION:
        linked_items: list[tuple[str, Page]] = []

        considerations = await db.get_considerations_for_question(page.id)
        for claim, link in considerations:
            if claim.id in _exclude:
                continue
            _link_highlighted = (
                highlight_run_id
                and link.run_id
                and link.run_id == highlight_run_id
                and claim.run_id != highlight_run_id
            )
            link_tag = " [LINKED BY THIS RUN]" if _link_highlighted else ""
            line = (
                "- "
                + await format_page(
                    claim,
                    linked_detail,
                    db=db,
                    linked_detail=None,
                    highlight_run_id=highlight_run_id,
                )
                + link_tag
            )
            if link.reasoning:
                line += f"\n  Reasoning: {link.reasoning}"
            linked_items.append((line, claim))

        judgements = await db.get_judgements_for_question(page.id)
        for j in judgements:
            if j.id in _exclude:
                continue
            line = "- " + await format_page(
                j,
                linked_detail,
                db=db,
                linked_detail=None,
                highlight_run_id=highlight_run_id,
            )
            linked_items.append((line, j))

        children = await db.get_child_questions(page.id)
        child_judgements = await db.get_judgements_for_questions(
            [child.id for child in children if child.id not in _exclude]
        )
        for child in children:
            if child.id in _exclude:
                continue
            for j in child_judgements.get(child.id, []):
                if j.id in _exclude:
                    continue
                line = (
                    f"- *On: {child.headline} (`{child.id[:8]}`)*  "
                    + await format_page(
                        j,
                        linked_detail,
                        db=db,
                        linked_detail=None,
                        highlight_run_id=highlight_run_id,
                    )
                )
                linked_items.append((line, j))

        if linked_items:
            lines.append("")
            lines.append(
                group_by_credence(linked_items, heading_level="####", separator="\n")
            )

    return "\n".join(lines)


async def render_view(
    view: Page,
    items_with_links: Sequence[tuple[Page, PageLink]],
    min_importance: int = 5,
) -> str:
    """Render a View page at the given importance threshold.

    - min_importance=5: NL summary only (no individual items)
    - min_importance=4: NL summary + programmatic rendering of all items at 4+
    - min_importance=3: NL summary + all items at 3+
    - min_importance=2: NL summary + all items at 2+

    Whenever items are rendered programmatically, importance-5 items are
    always included alongside lower-tier ones.
    """
    parts: list[str] = [f"## View: {view.headline}", ""]

    if view.content:
        parts.append(view.content)
        parts.append("")

    if min_importance >= 5:
        return "\n".join(parts)

    filtered = [
        (page, link)
        for page, link in items_with_links
        if link.importance is not None and link.importance >= min_importance
    ]
    if not filtered:
        return "\n".join(parts)

    sections_order = view.sections or []
    section_index = {s: i for i, s in enumerate(sections_order)}

    by_section: dict[str, list[tuple[Page, PageLink]]] = {}
    for page, link in filtered:
        sec = link.section or "other"
        by_section.setdefault(sec, []).append((page, link))

    ordered_sections = sorted(
        by_section.keys(),
        key=lambda s: section_index.get(s, 999),
    )

    parts.append("### View Items")
    parts.append("")
    for sec in ordered_sections:
        label = sec.replace("_", " ").title()
        parts.append(f"#### {label}")
        parts.append("")
        items = by_section[sec]
        items.sort(key=lambda pair: pair[1].position or 0)
        for page, link in items:
            imp = link.importance or 0
            c = page.credence if page.credence is not None else "?"
            r = page.robustness if page.robustness is not None else "?"
            parts.append(f"- [C{c}/R{r} I{imp}] `{page.id[:8]}` — {page.headline}")
            if page.content:
                for line in page.content.strip().split("\n"):
                    parts.append(f"  {line}")
            parts.append("")

    return "\n".join(parts)


async def render_child_investigation_results(
    db: DB,
    parent_question_id: str,
    last_view_created_at: datetime | None,
) -> tuple[str, list[str]]:
    """Render investigation results from child questions for the View updater.

    Returns (rendered_section_text, page_ids_used). If there are no child
    questions, returns ("", []).

    Detail level varies by newness (created after *last_view_created_at*):
    - NEW View: NL content + compact I4/I5 item headlines
    - Old View: NL content only
    - NEW Summary/Judgement: full content
    - Old Summary/Judgement: abstract only
    """
    children = await db.get_child_questions(parent_question_id)
    if not children:
        return "", []

    child_ids = [c.id for c in children]
    views_map, summaries_map, judgements_map = await asyncio.gather(
        db.get_views_for_questions(child_ids),
        db.get_latest_summaries_for_questions(child_ids),
        db.get_judgements_for_questions(child_ids),
    )

    def _is_new(page: Page) -> bool:
        if last_view_created_at is None:
            return True
        return page.created_at > last_view_created_at

    new_view_ids: list[str] = []
    for cid in child_ids:
        v = views_map.get(cid)
        if v and _is_new(v):
            new_view_ids.append(v.id)
    view_items_map: dict[str, list[tuple[Page, PageLink]]] = {}
    if new_view_ids:
        items_results = await asyncio.gather(
            *(db.get_view_items(vid, min_importance=4) for vid in new_view_ids)
        )
        for vid, items in zip(new_view_ids, items_results):
            view_items_map[vid] = items

    entries: list[tuple[bool, str, list[str]]] = []
    for child in children:
        cid = child.id
        view = views_map.get(cid)
        summary = summaries_map.get(cid)
        judgements = judgements_map.get(cid, [])
        latest_judgement = (
            max(judgements, key=lambda p: p.created_at) if judgements else None
        )

        new = False
        page_ids: list[str] = []
        lines: list[str] = [f"### `{cid[:8]}` — {child.headline}"]

        if view:
            new = _is_new(view)
            page_ids.append(view.id)
            lines.append(f"**Status:** View available{' [NEW]' if new else ''}")
            if view.content:
                lines.append("")
                lines.append(view.content)
            if new and view.id in view_items_map:
                items = view_items_map[view.id]
                if items:
                    lines.append("")
                    lines.append("**Key items:**")
                    for page, link in items:
                        imp = link.importance or 0
                        c = page.credence if page.credence is not None else "?"
                        r = page.robustness if page.robustness is not None else "?"
                        lines.append(
                            f"- [C{c}/R{r} I{imp}] `{page.id[:8]}` "
                            f"— {page.headline}"
                        )
                        page_ids.append(page.id)
        elif summary:
            new = _is_new(summary)
            page_ids.append(summary.id)
            lines.append(
                f"**Status:** Summary available{' [NEW]' if new else ''}"
            )
            lines.append("")
            if new:
                lines.append(summary.content or summary.abstract or "")
            else:
                lines.append(summary.abstract or "")
        elif latest_judgement:
            new = _is_new(latest_judgement)
            page_ids.append(latest_judgement.id)
            c = latest_judgement.credence if latest_judgement.credence is not None else "?"
            r = latest_judgement.robustness if latest_judgement.robustness is not None else "?"
            lines.append(
                f"**Status:** Judgement available{' [NEW]' if new else ''}"
            )
            lines.append(
                f"[JUDGEMENT C{c}/R{r}] {latest_judgement.headline}"
            )
            lines.append("")
            if new:
                lines.append(latest_judgement.content or latest_judgement.abstract or "")
            else:
                lines.append(latest_judgement.abstract or "")
        else:
            continue

        entries.append((new, "\n".join(lines), page_ids))

    if not entries:
        return "", []

    entries.sort(key=lambda e: (not e[0], e[1]))

    all_page_ids: list[str] = []
    parts = [
        "## Child Investigation Results",
        "",
        "The following sub-questions have been investigated. "
        "Items marked [NEW] have been updated since the last View revision.",
        "",
    ]
    for _, text, pids in entries:
        parts.append(text)
        parts.append("")
        all_page_ids.extend(pids)

    return "\n".join(parts), all_page_ids


async def _build_dependency_signal(db: DB) -> str | None:
    """Build a section listing the most-depended-on pages in the workspace.

    Returns None if no DEPENDS_ON links exist yet.
    """
    counts = await db.get_dependency_counts()
    if not counts:
        return None

    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]
    pages = await db.get_pages_by_ids([pid for pid, _ in top])
    lines = ["## Load-Bearing Pages (by dependency count)", ""]
    lines.append(
        "These pages are depended on by the most other pages. "
        "Prioritize them for robustness assessment — if they turn out to "
        "be wrong, the most downstream conclusions would be affected."
    )
    lines.append("")
    for pid, count in top:
        page = pages.get(pid)
        if page:
            stale_tag = " [SUPERSEDED]" if page.is_superseded else ""
            lines.append(
                f"- `{pid[:8]}` — {page.headline} ({count} dependents){stale_tag}"
            )
    return "\n".join(lines)


async def build_prioritization_context(
    db: DB,
    scope_question_id: str | None = None,
) -> tuple[str, dict[str, str]]:
    """Build context for a prioritization call.

    Includes the current View (importance 2+) for the scope question,
    then appends the scope question and its direct children (at ABSTRACT
    detail) and a dependency signal.

    Returns (context_text, short_id_map) where short_id_map maps 8-char
    short IDs to full UUIDs.
    """
    parts: list[str] = ["# Prioritization Context", ""]
    short_id_map: dict[str, str] = {}

    if scope_question_id:
        question = await db.get_page(scope_question_id)
        if question:
            view = await db.get_view_for_question(scope_question_id)
            if view:
                view_items = await db.get_view_items(
                    view.id, min_importance=2,
                )
                view_text = await render_view(
                    view, view_items, min_importance=2,
                )
                if view_text.strip():
                    parts.append(view_text)
                    parts.append("")
                    parts.append("---")
                    parts.append("")

            direct_children = await db.get_child_questions(scope_question_id)
            full_page_ids = {scope_question_id} | {c.id for c in direct_children}

            subtree_text = await render_page_and_immediate_children(
                scope_question_id,
                db,
                detail=PageDetail.ABSTRACT,
                linked_detail=PageDetail.ABSTRACT,
                content_page_ids=full_page_ids,
            )
            parts.append("## Scope Question — Detail")
            parts.append("")
            parts.append(subtree_text)
            parts.append("")

            for pid in full_page_ids:
                short_id_map[pid[:8]] = pid

    dep_section = await _build_dependency_signal(db)
    if dep_section:
        parts.append(dep_section)
        parts.append("")

    return "\n".join(parts), short_id_map


def _filter_summary_pages(
    ranked: Sequence[tuple[Page, float]],
) -> list[tuple[Page, float]]:
    return [(p, score) for p, score in ranked if p.page_type == PageType.SUMMARY]


async def build_embedding_based_context(
    question_text: str,
    db: DB,
    *,
    scope_question_id: str | None = None,
    scope_detail: PageDetail | None = None,
    scope_linked_detail: PageDetail | None = None,
    headline_only_ids: set[str] | None = None,
    full_page_char_budget: int | None = None,
    abstract_page_char_budget: int | None = None,
    summary_page_char_budget: int | None = None,
    distillation_page_char_budget: int | None = None,
    full_page_similarity_floor: float | None = None,
    abstract_page_similarity_floor: float | None = None,
    summary_page_similarity_floor: float | None = None,
    require_judgement_for_questions: bool = False,
    exclude_page_ids: set[str] | None = None,
) -> EmbeddingBasedContextResult:
    """Build context by embedding-similarity search over the whole workspace.

    Pages are ranked by similarity and placed into tiers by descending detail:
    distillation (CONTENT) -> full (CONTENT) -> abstract (ABSTRACT) -> summary
    (HEADLINE). Each tier has its own char budget and similarity floor.
    """
    settings = get_settings()
    if full_page_char_budget is None:
        full_page_char_budget = settings.full_page_char_budget
    if abstract_page_char_budget is None:
        abstract_page_char_budget = settings.abstract_page_char_budget
    if summary_page_char_budget is None:
        summary_page_char_budget = settings.summary_page_char_budget
    if distillation_page_char_budget is None:
        distillation_page_char_budget = settings.distillation_page_char_budget
    if full_page_similarity_floor is None:
        full_page_similarity_floor = settings.full_page_similarity_floor
    if abstract_page_similarity_floor is None:
        abstract_page_similarity_floor = settings.abstract_page_similarity_floor
    if summary_page_similarity_floor is None:
        summary_page_similarity_floor = settings.summary_page_similarity_floor

    match_threshold = min(
        full_page_similarity_floor,
        abstract_page_similarity_floor,
        summary_page_similarity_floor,
    )
    query_embedding = await embed_query(question_text)
    ranked = await search_pages_by_vector(
        db,
        query_embedding,
        match_threshold=match_threshold,
        match_count=500,
        field_name="abstract",
    )

    _exclude = exclude_page_ids or set()
    if _exclude:
        ranked = [(p, s) for p, s in ranked if p.id not in _exclude]

    scope_section = ""
    scope_page_ids: list[str] = []
    if scope_question_id:
        scope_page = await db.get_page(scope_question_id)
        if scope_page:
            scope_section = (
                "## Scope Question\n\n"
                + await format_page(
                    scope_page,
                    scope_detail or PageDetail.ABSTRACT,
                    linked_detail=scope_linked_detail or PageDetail.HEADLINE,
                    db=db,
                    exclude_page_ids=_exclude,
                )
                + "\n\n"
            )
            scope_page_ids = [scope_question_id]
            ranked = [(p, s) for p, s in ranked if p.id != scope_question_id]

    if require_judgement_for_questions:
        question_ids = [p.id for p, _ in ranked if p.page_type == PageType.QUESTION]
        judgements_by_qid = await db.get_judgements_for_questions(question_ids)
        has_judgement = {qid for qid, js in judgements_by_qid.items() if js}
        ranked = [
            (p, s)
            for p, s in ranked
            if p.page_type != PageType.QUESTION or p.id in has_judgement
        ]

    distillation_budget = distillation_page_char_budget
    full_budget = full_page_char_budget
    abstract_budget = abstract_page_char_budget
    summary_budget = summary_page_char_budget

    distillation_pages = _filter_summary_pages(ranked)
    distillation_ids: list[str] = []
    distillation_chars = 0
    all_items: list[tuple[str, Page]] = []
    full_ids: list[str] = []
    full_chars = 0
    abstract_ids: list[str] = []
    abstract_chars = 0
    summary_ids: list[str] = []
    summary_chars = 0

    _headline_only = headline_only_ids or set()
    distillation_page_id_set = {p.id for p, _ in distillation_pages}

    for page, _sim in distillation_pages:
        formatted = await format_page(
            page, PageDetail.CONTENT, db=db, linked_detail=None
        )
        if distillation_chars + len(formatted) <= distillation_budget:
            all_items.append((formatted, page))
            distillation_ids.append(page.id)
            distillation_chars += len(formatted)

    for page, _sim in ranked:
        if page.id in distillation_page_id_set:
            continue
        if page.id in _headline_only:
            formatted = await format_page(page, PageDetail.HEADLINE, linked_detail=None)
            all_items.append((formatted, page))
            summary_ids.append(page.id)
            summary_chars += len(formatted)
            continue

    for page, sim in ranked:
        if page.id in distillation_page_id_set:
            continue
        if page.id in _headline_only:
            continue

        if sim >= full_page_similarity_floor and full_chars < full_budget:
            formatted = await format_page(
                page, PageDetail.CONTENT, db=db, linked_detail=None
            )
            if full_chars + len(formatted) <= full_budget:
                all_items.append((formatted, page))
                full_ids.append(page.id)
                full_chars += len(formatted)
                continue

        if sim >= abstract_page_similarity_floor and abstract_chars < abstract_budget:
            formatted = await format_page(page, PageDetail.ABSTRACT, linked_detail=None)
            if abstract_chars + len(formatted) <= abstract_budget:
                all_items.append((formatted, page))
                abstract_ids.append(page.id)
                abstract_chars += len(formatted)
                continue

        if sim >= summary_page_similarity_floor and summary_chars < summary_budget:
            formatted = await format_page(page, PageDetail.HEADLINE, linked_detail=None)
            if summary_chars + len(formatted) <= summary_budget:
                all_items.append((formatted, page))
                summary_ids.append(page.id)
                summary_chars += len(formatted)
                continue

        if sim < summary_page_similarity_floor:
            break

    sections: list[str] = []
    if scope_section:
        sections.append(scope_section)
    if all_items:
        sections.append(group_by_credence(all_items, heading_level="##"))

    context_text = "\n".join(sections)

    budget_usage = {
        "distillation": distillation_chars,
        "full": full_chars,
        "abstract": abstract_chars,
        "summary": summary_chars,
    }
    log.info(
        "Embedding context: full=%d/%d chars, abstract=%d/%d chars, "
        "summary=%d/%d chars, distillation=%d/%d chars, "
        "pages=%d full + %d abstract + %d summary",
        full_chars,
        full_budget,
        abstract_chars,
        abstract_budget,
        summary_chars,
        summary_budget,
        distillation_chars,
        distillation_budget,
        len(full_ids),
        len(abstract_ids),
        len(summary_ids),
    )

    all_ids = scope_page_ids + distillation_ids + full_ids + abstract_ids + summary_ids
    return EmbeddingBasedContextResult(
        context_text=context_text,
        page_ids=all_ids,
        full_page_ids=full_ids,
        abstract_page_ids=abstract_ids,
        summary_page_ids=summary_ids,
        distillation_page_ids=distillation_ids,
        budget_usage=budget_usage,
    )
