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
from rumil.tracing.page_load_tracking import get_page_track_tags
from rumil.tracing.tracer import get_trace

log = logging.getLogger(__name__)


def format_epistemic_tag(page: Page) -> str:
    """Render "C{c}/R{r}" | "C{c}" | "R{r}" | "" for a page (null-safe)."""
    parts = []
    if page.credence is not None:
        parts.append(f"C{page.credence}")
    if page.robustness is not None:
        parts.append(f"R{page.robustness}")
    return "/".join(parts)


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
        return await format_page(
            root,
            detail,
            linked_detail=linked_detail,
            db=db,
            track=True,
            track_tags={"source": "prioritization"},
        )

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
            indent
            + await format_page(
                question,
                q_detail,
                linked_detail=None,
                db=db,
                track=True,
                track_tags={"source": "prioritization"},
            )
        )

        con_items: list[tuple[float, str]] = []
        for claim, link in considerations_by_q.get(question.id, []):
            visited.add(claim.id)
            direction = f"({link.direction.value}) " if link.direction else ""
            line = f"{indent}- {direction}" + await format_page(
                claim,
                linked_detail or PageDetail.HEADLINE,
                linked_detail=None,
                db=db,
                track=True,
                track_tags={"source": "prioritization"},
            )
            if link.reasoning:
                line += f"\n{indent}  Reasoning: {link.reasoning}"
            con_items.append((link.strength or 0.0, line))
        if con_items:
            parts.append("")
            hn = min(depth + 3, 6)
            parts.append(f"{'#' * hn} Considerations")
            con_items.sort(key=lambda x: x[0], reverse=True)
            parts.append("\n".join(line for _, line in con_items))

        all_judgements = judgements_by_q.get(question.id, [])
        judgements = [max(all_judgements, key=lambda j: j.created_at)] if all_judgements else []
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
                        track=True,
                        track_tags={"source": "prioritization"},
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
            notes.append(f"> **Note:** `[{sid}]` has been superseded (replacement not found).")
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
    track: bool = False,
    track_tags: dict[str, str] | None = None,
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

    When *track* is True, the page load is recorded via the ambient
    ``CallTrace`` (if one exists).  Ambient tags from ``page_track_scope``
    are merged with any explicit *track_tags* (explicit wins on conflict).
    Supersession replacement renders are not tracked separately (they
    represent the same logical page). Linked-item renders (considerations,
    judgements, child-question judgements for QUESTION pages) DO track
    when *track* is True, using source tags ``linked_consideration`` /
    ``linked_judgement`` / ``linked_child_judgement`` — this is how
    scope-question linked items become visible in ``page_format_events``.
    """
    if track:
        trace = get_trace()
        if trace:
            tags = {**get_page_track_tags(), **(track_tags or {})}
            trace.record_page_load(page.id, detail.value, tags)

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
            f"{original}\n\n> **SUPERSEDED** — this page has been replaced (replacement not found)."
        )

    if detail != PageDetail.HEADLINE and not page.content and db:
        full = await db.get_page(page.id)
        if full:
            page = full

    _is_highlighted = highlight_run_id and page.run_id and page.run_id == highlight_run_id

    if detail == PageDetail.HEADLINE:
        tag = f"{page.page_type.value.upper()}"
        ep = format_epistemic_tag(page)
        if ep:
            tag += f" {ep}"
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
        credence_line = f"Credence: {page.credence}/9"
        if page.credence_reasoning:
            credence_line += f" — {page.credence_reasoning}"
        lines.append(credence_line)
    if page.robustness is not None:
        robustness_line = f"Robustness: {page.robustness}/5"
        if page.robustness_reasoning:
            robustness_line += f" — {page.robustness_reasoning}"
        lines.append(robustness_line)
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
        sections: list[str] = []

        con_items: list[tuple[float, datetime, str]] = []
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
            rendered = await format_page(
                claim,
                linked_detail,
                db=db,
                linked_detail=None,
                highlight_run_id=highlight_run_id,
                track=track,
                track_tags={"source": "linked_consideration"},
            )
            line = "- " + rendered + link_tag
            if link.reasoning:
                line += f"\n  Reasoning: {link.reasoning}"
            con_items.append((link.strength or 0.0, claim.created_at, line))
        if con_items:
            con_items.sort(key=lambda x: (-x[0], x[1]))
            sections.append(
                "#### Considerations linked to this question\n\n"
                "_Claims the workspace has linked as bearing on this question._\n\n"
                + "\n".join(line for _, _, line in con_items)
            )

        j_items: list[tuple[datetime, str]] = []
        judgements = await db.get_judgements_for_question(page.id)
        for j in judgements:
            if j.id in _exclude:
                continue
            rendered = await format_page(
                j,
                linked_detail,
                db=db,
                linked_detail=None,
                highlight_run_id=highlight_run_id,
                track=track,
                track_tags={"source": "linked_judgement"},
            )
            j_items.append((j.created_at, "- " + rendered))
        if j_items:
            j_items.sort(key=lambda x: x[0])
            sections.append(
                "#### Judgements on this question\n\n"
                "_Standing answers the workspace has recorded for this question._\n\n"
                + "\n".join(line for _, line in j_items)
            )

        children = await db.get_child_questions(page.id)
        child_judgements = await db.get_judgements_for_questions(
            [child.id for child in children if child.id not in _exclude]
        )
        child_blocks: list[str] = []
        for child in children:
            if child.id in _exclude:
                continue
            child_js = [j for j in child_judgements.get(child.id, []) if j.id not in _exclude]
            if not child_js:
                continue
            sub_lines = [f"- On sub-question `{child.id[:8]}` — {child.headline}:"]
            for j in sorted(child_js, key=lambda x: x.created_at):
                rendered = await format_page(
                    j,
                    linked_detail,
                    db=db,
                    linked_detail=None,
                    highlight_run_id=highlight_run_id,
                    track=track,
                    track_tags={"source": "linked_child_judgement"},
                )
                sub_lines.append(f"  - {rendered}")
            child_blocks.append("\n".join(sub_lines))
        if child_blocks:
            sections.append(
                "#### Judgements on sub-questions\n\n"
                "_Answers recorded on questions nested under this one "
                "(not judgements of this question itself)._\n\n" + "\n".join(child_blocks)
            )

        if sections:
            lines.append("")
            lines.append("\n\n".join(sections))

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
            r = page.robustness if page.robustness is not None else "?"
            parts.append(f"- [R{r} I{imp}] `{page.id[:8]}` — {page.headline}")
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
    from rumil.views import get_active_view

    children = await db.get_child_questions(parent_question_id)
    if not children:
        return "", []

    child_ids = [c.id for c in children]
    summaries_map = await db.get_latest_summaries_for_questions(child_ids)

    def _is_new(page: Page) -> bool:
        if last_view_created_at is None:
            return True
        return page.created_at > last_view_created_at

    view = get_active_view()
    view_renders = await asyncio.gather(
        *(
            view.render_for_child_investigation_results(
                cid, db, last_view_created_at=last_view_created_at
            )
            for cid in child_ids
        )
    )

    entries: list[tuple[bool, str, list[str]]] = []
    for child, view_render in zip(children, view_renders):
        cid = child.id
        header = f"### `{cid[:8]}` — {child.headline}"

        if view_render is not None:
            is_new, rendered, page_ids = view_render
            entries.append((is_new, header + "\n" + rendered, list(page_ids)))
            continue

        summary = summaries_map.get(cid)
        if not summary:
            continue

        new = _is_new(summary)
        detail = PageDetail.CONTENT if new else PageDetail.ABSTRACT
        lines = [
            header,
            f"**Status:** Summary available{' [NEW]' if new else ''}",
            "",
            await format_page(
                summary,
                detail,
                linked_detail=None,
                db=db,
                track=True,
                track_tags={"source": "child_investigation"},
            ),
        ]
        entries.append((new, "\n".join(lines), [summary.id]))

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

    Returns None if there are no renderable load-bearing pages — either
    because no DEPENDS_ON links exist, or because none of the top pages
    could be resolved.
    """
    counts = await db.get_dependency_counts()
    if not counts:
        return None

    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]
    pages = await db.get_pages_by_ids([pid for pid, _ in top])

    item_lines: list[str] = []
    for pid, count in top:
        page = pages.get(pid)
        if page:
            headline = await format_page(
                page,
                PageDetail.HEADLINE,
                db=db,
                track=True,
                track_tags={"source": "dependency_signal"},
            )
            item_lines.append(f"- {headline} ({count} dependents)")

    if not item_lines:
        return None

    lines = [
        "## Load-Bearing Pages (by dependency count)",
        "",
        (
            "These pages are depended on by the most other pages. "
            "Prioritize them for robustness assessment — if they turn out to "
            "be wrong, the most downstream conclusions would be affected."
        ),
        "",
    ]
    lines.extend(item_lines)
    return "\n".join(lines)


async def build_prioritization_context(
    db: DB,
    scope_question_id: str | None = None,
    *,
    current_call_id: str | None = None,
) -> tuple[str, dict[str, str]]:
    """Build context for a prioritization call.

    Includes the current View (importance 2+) for the scope question,
    then appends the scope question and its direct children (at ABSTRACT
    detail), a dependency signal, and (when ``scope_question_id`` is set)
    a coordination section listing other in-flight calls on the scope
    question and active prioritisation pools on its subquestions.

    The coordination section is placed last so it doesn't interfere with
    the cacheable prefix above it.

    Returns (context_text, short_id_map) where short_id_map maps 8-char
    short IDs to full UUIDs.
    """
    parts: list[str] = ["# Prioritization Context", ""]
    short_id_map: dict[str, str] = {}

    if scope_question_id:
        from rumil.views import get_active_view

        question = await db.get_page(scope_question_id)
        if question:
            view_text = await get_active_view().render_for_prioritization(scope_question_id, db)
            if view_text:
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

    if scope_question_id:
        coord_section = await _build_coordination_section(
            db,
            scope_question_id,
            current_call_id=current_call_id,
        )
        if coord_section:
            parts.append(coord_section)
            parts.append("")

    return "\n".join(parts), short_id_map


async def _build_coordination_section(
    db: DB,
    scope_question_id: str,
    *,
    current_call_id: str | None,
) -> str:
    """Render the coordination section for prio prompts.

    Shows in-flight calls on the scope question (with assigned budgets) and
    active prio pools on subquestions (with remaining budget). Returns an
    empty string when nothing is in flight, so the section is omitted.
    """
    in_flight = await db.get_active_calls_for_question(
        scope_question_id,
        exclude_call_id=current_call_id,
    )
    sub_pools = await db.get_active_prio_pools_for_subquestions(scope_question_id)

    if not in_flight and not sub_pools:
        return ""

    lines: list[str] = ["## Coordination: in-flight work on this question and its subquestions", ""]

    if in_flight:
        lines.append("### Calls in flight against this question")
        lines.append("")
        lines.append(
            "Other calls currently dispatched against this question (excluding "
            "your own). Their assigned budgets contribute to the same shared "
            "pool you're drawing from — your budget line above already accounts "
            "for what they've consumed:"
        )
        lines.append("")
        for c in in_flight:
            assigned = (
                f"assigned budget {c.budget_allocated}"
                if c.budget_allocated is not None
                else "assigned budget —"
            )
            lines.append(f"- `[{c.id[:8]}]` {c.call_type.value.upper()} — {assigned}")
        lines.append("")

    if sub_pools:
        sub_ids = [sub_id for sub_id, _ in sub_pools]
        sub_pages = await db.get_pages_by_ids(sub_ids)
        lines.append("### Active prioritisation cycles on subquestions")
        lines.append("")
        for sub_id, sub_pool in sub_pools:
            page = sub_pages.get(sub_id)
            headline = page.headline if page else ""
            cycles_label = (
                "1 active cycle"
                if sub_pool.active_calls == 1
                else f"{sub_pool.active_calls} active cycles"
            )
            lines.append(
                f'- `[{sub_id[:8]}]` "{headline}" — '
                f"**{max(sub_pool.remaining, 0)} budget remaining** "
                f"({cycles_label})"
            )
        lines.append("")
        lines.append(
            "If you want to wait for one of these subquestion investigations to "
            "finish before assessing this question, recurse into that "
            "subquestion. If you want to wait without doing additional work "
            "beyond what the running cycle is already doing, recurse with the "
            "minimum allowed budget — your contribution will marginally extend "
            "the running investigation but will block until the subquestion's "
            "pool is exhausted."
        )

    return "\n".join(lines)


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
    input_type: str = "query",
) -> EmbeddingBasedContextResult:
    """Build context by embedding-similarity search over the whole workspace.

    Pages are ranked by similarity and placed into tiers by descending detail:
    distillation (CONTENT) -> full (CONTENT) -> abstract (ABSTRACT) -> summary
    (HEADLINE). Each tier has its own char budget and similarity floor.

    ``input_type`` defaults to ``"query"`` for the common case where the
    caller passes a question headline to find pages that bear on it. Pass
    ``"document"`` when the query text IS itself a page (e.g. a claim being
    reassessed) — this gives symmetric page-to-page similarity instead of
    the asymmetric question-to-document similarity Voyage uses by default.

    When ``input_type="document"``, default tier floors are shifted up by
    ``settings.document_floor_delta`` to account for the score-distribution
    shift — identical text scores ~0.74 under query-mode but ~1.00 under
    document-mode against stored documents. Explicit floor overrides are
    used as-passed and not shifted.
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
    delta = settings.document_floor_delta if input_type == "document" else 0.0
    if full_page_similarity_floor is None:
        full_page_similarity_floor = settings.full_page_similarity_floor + delta
    if abstract_page_similarity_floor is None:
        abstract_page_similarity_floor = settings.abstract_page_similarity_floor + delta
    if summary_page_similarity_floor is None:
        summary_page_similarity_floor = settings.summary_page_similarity_floor + delta

    match_threshold = min(
        full_page_similarity_floor,
        abstract_page_similarity_floor,
        summary_page_similarity_floor,
    )
    query_embedding = await embed_query(question_text, input_type=input_type)
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
                    track=True,
                    track_tags={"source": "scope_question"},
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
            (p, s) for p, s in ranked if p.page_type != PageType.QUESTION or p.id in has_judgement
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
            page,
            PageDetail.CONTENT,
            db=db,
            linked_detail=None,
            track=True,
            track_tags={"source": "embedding_distillation"},
        )
        if distillation_chars + len(formatted) <= distillation_budget:
            all_items.append((formatted, page))
            distillation_ids.append(page.id)
            distillation_chars += len(formatted)

    for page, _sim in ranked:
        if page.id in distillation_page_id_set:
            continue
        if page.id in _headline_only:
            formatted = await format_page(
                page,
                PageDetail.HEADLINE,
                linked_detail=None,
                track=True,
                track_tags={"source": "embedding_headline_override"},
            )
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
                page,
                PageDetail.CONTENT,
                db=db,
                linked_detail=None,
                track=True,
                track_tags={"source": "embedding_full"},
            )
            if full_chars + len(formatted) <= full_budget:
                all_items.append((formatted, page))
                full_ids.append(page.id)
                full_chars += len(formatted)
                continue

        if sim >= abstract_page_similarity_floor and abstract_chars < abstract_budget:
            formatted = await format_page(
                page,
                PageDetail.ABSTRACT,
                linked_detail=None,
                track=True,
                track_tags={"source": "embedding_abstract"},
            )
            if abstract_chars + len(formatted) <= abstract_budget:
                all_items.append((formatted, page))
                abstract_ids.append(page.id)
                abstract_chars += len(formatted)
                continue

        if sim >= summary_page_similarity_floor and summary_chars < summary_budget:
            formatted = await format_page(
                page,
                PageDetail.HEADLINE,
                linked_detail=None,
                track=True,
                track_tags={"source": "embedding_summary"},
            )
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
        sections.append("\n\n".join(text for text, _ in all_items))

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
