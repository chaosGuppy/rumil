"""
Build context text from workspace pages for injection into LLM prompts.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from rumil.database import DB
from rumil.embeddings import embed_query, search_pages_by_vector
from rumil.models import LinkRole, Page, PageType, Workspace
from rumil.page_graph import PageGraph
from rumil.workspace_map import build_workspace_map
from rumil.settings import get_settings

log = logging.getLogger(__name__)


@dataclass
class EmbeddingBasedContextResult:
    context_text: str
    page_ids: list[str]
    full_page_ids: list[str]
    summary_page_ids: list[str]
    distillation_page_ids: list[str] = field(default_factory=list)
    budget_usage: dict[str, int] = field(default_factory=dict)


async def collect_subtree_ids(
    question_id: str,
    db: DB,
    graph: PageGraph | None = None,
    _visited: set[str] | None = None,
) -> set[str]:
    """Recursively collect all question IDs in a subtree (inclusive)."""
    if _visited is None:
        _visited = set()
    if question_id in _visited:
        return set()
    _visited = _visited | {question_id}
    source: DB | PageGraph = graph if graph is not None else db
    result = {question_id}
    for child in await source.get_child_questions(question_id):
        result |= await collect_subtree_ids(child.id, db, graph=graph, _visited=_visited)
    return result


async def format_page(
    page: Page,
    db: DB | None = None,
    graph: PageGraph | None = None,
) -> str:
    """Format a single page as readable text for LLM context."""
    source: DB | PageGraph | None = graph if graph is not None else db
    extra = page.extra or {}
    lines = [
        f"### [{page.page_type.value.upper()}] {page.headline}",
        f"ID: {page.id}",
        f"Epistemic status: {page.epistemic_status:.1f}/5 ({page.epistemic_type})",
    ]

    for k, v in extra.items():
        lines.append(f"{k}: {v}")

    lines += ["", page.content]

    if source and page.page_type == PageType.QUESTION:
        considerations = await source.get_considerations_for_question(page.id)
        if considerations:
            lines.append("")
            lines.append("**Considerations:**")
            for claim, link in considerations:
                lines.append(
                    f"- [strength {link.strength:.1f}/5] "
                    f"{claim.headline} (ID: {claim.id})"
                )
                if link.reasoning:
                    lines.append(f"  Reasoning: {link.reasoning}")

        judgements = await source.get_judgements_for_question(page.id)
        if judgements:
            lines.append("")
            lines.append("**Existing judgements:**")
            for j in judgements:
                lines.append(f"- {j.headline} (confidence: {j.epistemic_status:.1f}/5)")

    return "\n".join(lines)


async def format_pages_block(
    pages: list[Page],
    header: str,
    db: DB | None = None,
    graph: PageGraph | None = None,
) -> str:
    if not pages:
        return ""
    parts = [f"## {header}", ""]
    for page in pages:
        parts.append(await format_page(page, db=db, graph=graph))
        parts.append("")
    return "\n".join(parts)


async def build_context_for_question(
    question_id: str,
    db: DB,
    include_considerations: bool = True,
    include_judgements: bool = True,
    graph: PageGraph | None = None,
) -> tuple[str, list[str]]:
    """Build full context text for working on a question.

    Returns (context_text, loaded_page_ids) where loaded_page_ids lists every
    page whose full content was included.
    """
    source: DB | PageGraph = graph if graph is not None else db
    question = await source.get_page(question_id)
    if not question:
        return f"[Question {question_id} not found]", []

    loaded_ids = [question_id]
    parts = ["# Workspace Context", ""]
    parts.append(await format_page(question, db=db, graph=graph))
    parts.append("")

    if include_considerations:
        considerations = await source.get_considerations_for_question(question_id)
        if considerations:
            parts.append("## Existing Considerations")
            parts.append("")
            for claim, link in considerations:
                loaded_ids.append(claim.id)
                parts.append(
                    f"**[strength {link.strength:.1f}/5]** "
                    f"{claim.headline} (ID: `{claim.id}`)"
                )
                parts.append(claim.content)
                if link.reasoning:
                    parts.append(f"*Link reasoning: {link.reasoning}*")
                parts.append("")

    if include_judgements:
        judgements = await source.get_judgements_for_question(question_id)
        if judgements:
            parts.append("## Existing Judgements")
            parts.append("")
            for j in judgements:
                loaded_ids.append(j.id)
                parts.append(await format_page(j))
                parts.append("")

        children = await source.get_child_questions(question_id)
        child_judgements = []
        for child in children:
            for j in await source.get_judgements_for_question(child.id):
                child_judgements.append((child, j))
        if child_judgements:
            parts.append("## Sub-question Judgements")
            parts.append("")
            for child, j in child_judgements:
                loaded_ids.append(j.id)
                parts.append(f"*On sub-question: {child.headline} (`{child.id}`)*")
                parts.append(await format_page(j))
                parts.append("")

    return "\n".join(parts), loaded_ids


async def build_call_context(
    question_id: str,
    db: DB,
    extra_page_ids: list[str] | None = None,
    graph: PageGraph | None = None,
) -> tuple[str, dict[str, str], list[str]]:
    """Build full context for an assess/ingest call.

    Prepends a compact workspace map, then the detailed working context for
    the given question. Any extra_page_ids (full UUIDs) are appended as
    pre-loaded pages at the end.

    Returns (context_text, short_id_to_full_uuid, working_context_page_ids).
    """
    source: DB | PageGraph = graph if graph is not None else db
    log.debug("build_call_context: question=%s", question_id[:8])
    map_text, short_id_map = await build_workspace_map(db, graph=graph)
    working_context, working_page_ids = await build_context_for_question(
        question_id, db, graph=graph,
    )

    parts = [
        map_text,
        "---",
        "",
        "## Working Context",
        "",
        working_context,
    ]

    if extra_page_ids:
        for pid in extra_page_ids:
            page = await source.get_page(pid)
            if page:
                parts += ["", "---", "", f"## Pre-loaded Page: `{pid[:8]}`", ""]
                parts.append(await format_page(page, db=db, graph=graph))

    context_text = "\n".join(parts)
    log.debug(
        "build_call_context complete: %d chars, %d working pages, %d extra pages",
        len(context_text), len(working_page_ids), len(extra_page_ids or []),
    )
    return context_text, short_id_map, working_page_ids


async def format_question_for_scout(
    question_id: str,
    db: DB,
    graph: PageGraph | None = None,
) -> tuple[str, list[str]]:
    """Build scout working context with role-aware display.

    Direct considerations/children are shown compactly (summary only).
    Structural ones are shown expanded (full content).
    Judgements are always expanded.

    Returns (context_text, loaded_page_ids).
    """
    source: DB | PageGraph = graph if graph is not None else db
    question = await source.get_page(question_id)
    if not question:
        return f"[Question {question_id} not found]", []

    loaded_ids = [question_id]
    parts = ["# Scope Question", ""]
    parts.append(await format_page(question))
    parts.append("")

    considerations = await source.get_considerations_for_question(question_id)
    direct_cons = [(p, l) for p, l in considerations if l.role == LinkRole.DIRECT]
    structural_cons = [(p, l) for p, l in considerations if l.role == LinkRole.STRUCTURAL]

    children_with_links = await source.get_child_questions_with_links(question_id)
    direct_children = [(p, l) for p, l in children_with_links if l.role == LinkRole.DIRECT]
    structural_children = [(p, l) for p, l in children_with_links if l.role == LinkRole.STRUCTURAL]

    if direct_cons or direct_children:
        parts.append("## Direct Considerations (compact)")
        parts.append(
            "These pages directly bear on the answer. They are shown in compact form "
            "so you know what ground is already covered -- avoid redundant claims."
        )
        parts.append("")
        for claim, link in direct_cons:
            loaded_ids.append(claim.id)
            parts.append(
                f"- [strength {link.strength:.1f}] {claim.headline} (ID: {claim.id})"
            )
        for child, link in direct_children:
            loaded_ids.append(child.id)
            parts.append(f"- [sub-Q] {child.headline} (ID: {child.id})")
        parts.append("")

    if structural_cons or structural_children:
        parts.append("## Structural Considerations (expanded)")
        parts.append(
            "These pages frame the investigation -- they indicate what evidence and "
            "angles still need to be explored. Read them to understand what bears "
            "on the question and in which direction."
        )
        parts.append("")
        for claim, link in structural_cons:
            loaded_ids.append(claim.id)
            parts.append(f"### [{claim.page_type.value.upper()}] {claim.headline}")
            parts.append(f"ID: {claim.id}")
            parts.append(f"Strength: {link.strength:.1f}/5")
            parts.append("")
            parts.append(claim.content)
            parts.append("")
        for child, link in structural_children:
            loaded_ids.append(child.id)
            parts.append(f"### [QUESTION] {child.headline}")
            parts.append(f"ID: {child.id}")
            parts.append("")
            parts.append(child.content)
            parts.append("")

    judgements = await source.get_judgements_for_question(question_id)
    if judgements:
        parts.append("## Existing Judgements")
        parts.append("")
        for j in judgements:
            loaded_ids.append(j.id)
            parts.append(await format_page(j))
            parts.append("")

    children = await source.get_child_questions(question_id)
    child_judgements = []
    for child in children:
        for j in await source.get_judgements_for_question(child.id):
            child_judgements.append((child, j))
    if child_judgements:
        parts.append("## Sub-question Judgements")
        parts.append("")
        for child, j in child_judgements:
            loaded_ids.append(j.id)
            parts.append(f"*On sub-question: {child.headline} (`{child.id}`)*")
            parts.append(await format_page(j))
            parts.append("")

    return "\n".join(parts), loaded_ids


async def _build_question_index(
    question_id: str,
    db: DB,
    indent: int = 0,
    graph: PageGraph | None = None,
    _visited: set[str] | None = None,
) -> list[str]:
    """Recursively build a flat index of all questions in the tree with their IDs.
    Includes consideration count, last scout fruit/date, and hypothesis flag."""
    if _visited is None:
        _visited = set()
    if question_id in _visited:
        return [f"{'  ' * indent}[child] `{question_id}` — *** cycle detected ***"]
    _visited = _visited | {question_id}

    source: DB | PageGraph = graph if graph is not None else db
    question = await source.get_page(question_id)
    if not question:
        return []
    prefix = "  " * indent
    tag = "[scope]" if indent == 0 else "[child]"

    extra = question.extra or {}
    is_hypothesis = extra.get("hypothesis", False)
    hypothesis_tag = " [hypothesis]" if is_hypothesis else ""

    n_cons = len(await source.get_considerations_for_question(question_id))
    scout_info = await db.get_last_scout_info(question_id)
    if scout_info:
        date_str = scout_info[0][:10]
        fruit = scout_info[1]
        fruit_str = f"fruit={fruit}" if fruit is not None else "fruit=?"
        scout_str = f"{fruit_str} · {date_str}"
    else:
        scout_str = "never scouted"

    lines = [
        f"{prefix}{tag}{hypothesis_tag} `{question_id}` — {question.headline} "
        f"({n_cons} cons · {scout_str})"
    ]
    for child in await source.get_child_questions(question_id):
        lines.extend(await _build_question_index(
            child.id, db, indent + 1, graph=graph, _visited=_visited,
        ))
    return lines


def assemble_call_context(
    working_context: str,
    workspace_map: str | None = None,
    extra_pages_text: str | None = None,
) -> str:
    """Assemble context text from pre-built components.

    Pure string operation -- no DB dependency. Called separately for each phase
    of a call (initial page loading, main call, closing review) with different
    workspace maps.
    """
    parts: list[str] = []
    if workspace_map:
        parts.append(workspace_map)
        parts.append("---")
        parts.append("")
    parts.append("## Working Context")
    parts.append("")
    parts.append(working_context)
    if extra_pages_text:
        parts.append("")
        parts.append("## Loaded Pages")
        parts.append("")
        parts.append(extra_pages_text)
    return "\n".join(parts)


async def format_preloaded_pages(
    page_ids: list[str],
    db: DB,
    graph: PageGraph | None = None,
) -> str:
    """Format preloaded pages as context text."""
    source: DB | PageGraph = graph if graph is not None else db
    parts: list[str] = []
    for pid in page_ids:
        page = await source.get_page(pid)
        if page:
            parts += ["---", "", f"## Pre-loaded Page: `{pid[:8]}`", ""]
            parts.append(await format_page(page, db=db, graph=graph))
            parts.append("")
    return "\n".join(parts)


async def build_prioritization_context(
    db: DB,
    scope_question_id: str | None = None,
    graph: PageGraph | None = None,
) -> tuple[str, dict[str, str]]:
    """Build context for a prioritization call.

    Returns (context_text, short_id_map) where short_id_map maps 8-char
    short IDs to full UUIDs.
    """
    source: DB | PageGraph = graph if graph is not None else db
    map_text, short_id_map = await build_workspace_map(db, graph=graph)
    parts = [map_text, "", "---", "", "# Prioritization Context", ""]

    if scope_question_id:
        question = await source.get_page(scope_question_id)
        if question:
            index_lines = await _build_question_index(
                scope_question_id, db, graph=graph,
            )
            parts.append("## Scope Subtree — Dispatchable Questions")
            parts.append("")
            parts.append(
                "You can only dispatch research calls on questions in this subtree "
                "(or on new subquestions you create during this call). "
                "Use only these exact IDs in your dispatch tags:"
            )
            parts.append("")
            parts.extend(index_lines)
            parts.append("")

            parts.append("## Scope Question")
            parts.append("")
            parts.append(await format_page(question, db=db, graph=graph))
            parts.append("")

            children = await source.get_child_questions(scope_question_id)
            if children:
                parts.append("## Sub-questions")
                parts.append("")
                for child in children:
                    parts.append(await format_page(child, db=db, graph=graph))
                    parts.append("")

    source_pages = await source.get_pages(page_type=PageType.SOURCE)
    if source_pages:
        ingest_history = await db.get_ingest_history()
        parts.append("## Sources and Ingest History")
        parts.append("")
        for src in source_pages:
            src_extra = src.extra or {}
            filename = src_extra.get("filename", src.id[:8])
            char_count = src_extra.get("char_count", len(src.content))
            question_ids = ingest_history.get(src.id, [])
            parts.append(f"[SRC] `{src.id[:8]}` — {filename} ({char_count:,} chars)")
            if question_ids:
                for qid in question_ids:
                    q = await source.get_page(qid)
                    q_summary = q.headline[:60] if q else qid[:8]
                    parts.append(f"  Ingested for: `{qid[:8]}` — {q_summary}")
            else:
                parts.append("  Not yet ingested for any question")
        parts.append("")

    return "\n".join(parts), short_id_map


def _format_page_summary(page: Page) -> str:
    """Single-line compact format for a page."""
    e = f"{page.epistemic_status:.0f}" if page.epistemic_status is not None else "?"
    return (
        f'[{page.page_type.value.upper()} {e}/5] '
        f'`{page.id[:8]}` -- {page.headline}'
    )


def _filter_distillation_pages(
    ranked: Sequence[tuple[Page, float]],
) -> list[tuple[Page, float]]:
    """Filter pages eligible for the distillation tier (stub)."""
    return []


async def build_embedding_based_context(
    question_text: str,
    db: DB,
    *,
    scope_question_id: str | None = None,
    context_char_budget: int | None = None,
    distillation_page_char_fraction: float | None = None,
    full_page_char_fraction: float | None = None,
    summary_para_char_fraction: float | None = None,
    match_threshold: float = 0.3,
) -> EmbeddingBasedContextResult:
    """Build context by embedding-similarity search over the whole workspace.

    Budget parameters default to values from settings when not provided.
    """
    settings = get_settings()
    if context_char_budget is None:
        context_char_budget = settings.context_char_budget
    if distillation_page_char_fraction is None:
        distillation_page_char_fraction = settings.distillation_page_char_fraction
    if full_page_char_fraction is None:
        full_page_char_fraction = settings.full_page_char_fraction
    if summary_para_char_fraction is None:
        summary_para_char_fraction = settings.summary_page_char_fraction
    query_embedding = await embed_query(question_text)
    ranked = await search_pages_by_vector(
        db,
        query_embedding,
        match_threshold=match_threshold,
        match_count=500,
        field_name='abstract',
    )

    scope_section = ''
    scope_page_ids: list[str] = []
    if scope_question_id:
        scope_page = await db.get_page(scope_question_id)
        if scope_page:
            scope_section = (
                '## Scope Question\n\n'
                + await format_page(scope_page, db)
                + '\n\n'
            )
            scope_page_ids = [scope_question_id]
            ranked = [(p, s) for p, s in ranked if p.id != scope_question_id]

    distillation_budget = int(context_char_budget * distillation_page_char_fraction)
    full_budget = int(context_char_budget * full_page_char_fraction)
    summary_budget = int(context_char_budget * summary_para_char_fraction)

    distillation_pages = _filter_distillation_pages(ranked)
    distillation_ids: list[str] = []
    distillation_chars = 0
    full_parts: list[str] = []
    full_ids: list[str] = []
    full_chars = 0
    summary_parts: list[str] = []
    summary_ids: list[str] = []
    summary_chars = 0

    distillation_page_id_set = {p.id for p, _ in distillation_pages}
    for page, _sim in distillation_pages:
        formatted = await format_page(page, db)
        if distillation_chars + len(formatted) <= distillation_budget:
            full_parts.append(formatted)
            distillation_ids.append(page.id)
            distillation_chars += len(formatted)

    for page, _sim in ranked:
        if page.id in distillation_page_id_set:
            continue

        if full_chars < full_budget:
            formatted = await format_page(page, db)
            if full_chars + len(formatted) <= full_budget:
                full_parts.append(formatted)
                full_ids.append(page.id)
                full_chars += len(formatted)
                continue

        if summary_chars < summary_budget:
            line = _format_page_summary(page)
            if summary_chars + len(line) <= summary_budget:
                summary_parts.append(line)
                summary_ids.append(page.id)
                summary_chars += len(line)
                continue

        break

    sections: list[str] = []
    if scope_section:
        sections.append(scope_section)
    if full_parts:
        sections.append('## Relevant Pages (Full)')
        sections.append('')
        sections.append('\n\n'.join(full_parts))
    if summary_parts:
        sections.append('')
        sections.append('## Relevant Pages (Summaries)')
        sections.append('')
        sections.append('\n'.join(summary_parts))

    context_text = '\n'.join(sections)

    budget_usage = {
        'distillation': distillation_chars,
        'full': full_chars,
        'summary': summary_chars,
    }
    log.info(
        'Embedding context: full=%d/%d chars, summary=%d/%d chars, '
        'distillation=%d/%d chars, pages=%d full + %d summary',
        full_chars, full_budget, summary_chars, summary_budget,
        distillation_chars, distillation_budget,
        len(full_ids), len(summary_ids),
    )

    all_ids = scope_page_ids + distillation_ids + full_ids + summary_ids
    return EmbeddingBasedContextResult(
        context_text=context_text,
        page_ids=all_ids,
        full_page_ids=full_ids,
        summary_page_ids=summary_ids,
        distillation_page_ids=distillation_ids,
        budget_usage=budget_usage,
    )
