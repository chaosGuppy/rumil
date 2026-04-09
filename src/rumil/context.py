"""
Build context text from workspace pages for injection into LLM prompts.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from rumil.database import DB
from rumil.embeddings import embed_query, search_pages_by_vector
from rumil.models import LinkRole, Page, PageDetail, PageType
from rumil.page_graph import PageGraph
from rumil.workspace_map import build_workspace_map
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


async def collect_all_subtree_page_ids(
    question_id: str,
    db: DB,
    graph: PageGraph | None = None,
    _visited: set[str] | None = None,
) -> set[str]:
    """Recursively collect all page IDs in a subtree: questions, considerations, and judgements."""
    if _visited is None:
        _visited = set()
    if question_id in _visited:
        return set()
    _visited = _visited | {question_id}
    source: DB | PageGraph = graph if graph is not None else db
    result: set[str] = {question_id}
    for page, _ in await source.get_considerations_for_question(question_id):
        result.add(page.id)
    for j in await source.get_judgements_for_question(question_id):
        result.add(j.id)
    for child in await source.get_child_questions(question_id):
        result |= await collect_all_subtree_page_ids(child.id, db, graph=graph, _visited=_visited)
    return result


async def _get_ancestry_chain(
    question_id: str,
    source: DB | PageGraph,
) -> list[Page]:
    """Walk up from question to root. Returns [parent, grandparent, ...] order."""
    chain: list[Page] = []
    current_id = question_id
    visited = {question_id}
    while True:
        parent = await source.get_parent_question(current_id)
        if not parent or parent.id in visited:
            break
        chain.append(parent)
        visited.add(parent.id)
        current_id = parent.id
    return chain


async def _render_subtree_headlines(
    question_id: str,
    source: DB | PageGraph,
    db: DB,
    indent: int = 0,
    _visited: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Render a question subtree as headlines. Returns (lines, page_ids)."""
    if _visited is None:
        _visited = set()
    if question_id in _visited:
        return [], []
    _visited = _visited | {question_id}

    question = await source.get_page(question_id)
    if not question:
        return [], []

    prefix = '  ' * indent
    lines = [prefix + await format_page(question, PageDetail.HEADLINE)]
    page_ids = [question_id]

    judgements = await source.get_judgements_for_question(question_id)
    if judgements:
        latest = max(judgements, key=lambda j: j.created_at)
        lines.append(
            prefix + '  ' + await format_page(latest, PageDetail.HEADLINE)
        )
        page_ids.append(latest.id)

    for child in await source.get_child_questions(question_id):
        child_lines, child_ids = await _render_subtree_headlines(
            child.id, source, db, indent + 1, _visited=_visited,
        )
        lines.extend(child_lines)
        page_ids.extend(child_ids)

    return lines, page_ids


@dataclass
class ScoutContextResult:
    context_text: str
    page_ids: list[str]
    structural_page_ids: set[str]


async def build_scout_context(
    question_id: str,
    db: DB,
    graph: PageGraph | None = None,
) -> ScoutContextResult:
    """Build context for a find_considerations call.

    Combines embedding search with structural context:
    - Full text of scope question
    - Abstract of parent/grandparent + direct child considerations/questions/judgements
    - Headlines for ancestry chain, siblings, subtree, and judgements on each
    """
    source: DB | PageGraph = graph if graph is not None else db
    question = await source.get_page(question_id)
    if not question:
        return ScoutContextResult(
            context_text=f'[Question {question_id} not found]',
            page_ids=[], structural_page_ids=set(),
        )

    all_page_ids: list[str] = []
    structural_ids: set[str] = set()
    parts: list[str] = []

    parts.append('# Scope Question')
    parts.append('')
    parts.append(await format_page(question, PageDetail.CONTENT, db=db, linked_detail=None))
    parts.append('')
    all_page_ids.append(question_id)
    structural_ids.add(question_id)

    ancestry = await _get_ancestry_chain(question_id, source)
    parent = ancestry[0] if len(ancestry) >= 1 else None
    grandparent = ancestry[1] if len(ancestry) >= 2 else None

    if parent or grandparent:
        parts.append('# Parent Context')
        parts.append('')
        if parent:
            parts.append('## Parent Question')
            parts.append('')
            parts.append(await format_page(parent, PageDetail.ABSTRACT, db=db, linked_detail=None))
            parts.append('')
            all_page_ids.append(parent.id)
            structural_ids.add(parent.id)
        if grandparent:
            parts.append('## Grandparent Question')
            parts.append('')
            parts.append(await format_page(grandparent, PageDetail.ABSTRACT, db=db, linked_detail=None))
            parts.append('')
            all_page_ids.append(grandparent.id)
            structural_ids.add(grandparent.id)

    considerations = await source.get_considerations_for_question(question_id)
    children = await source.get_child_questions(question_id)
    child_judgements_by_qid = await source.get_judgements_for_questions(
        [c.id for c in children]
    )
    child_judgements: list[tuple[Page, Page]] = [
        (child, j)
        for child in children
        for j in child_judgements_by_qid.get(child.id, [])
    ]

    if considerations or children or child_judgements:
        direct_items: list[tuple[str, Page]] = []
        for claim, link in considerations:
            direction = f' **({link.direction.value})**\n' if link.direction else ''
            formatted = direction + await format_page(claim, PageDetail.ABSTRACT, db=db, linked_detail=None)
            direct_items.append((formatted, claim))
            all_page_ids.append(claim.id)
            structural_ids.add(claim.id)
        for child in children:
            formatted = await format_page(child, PageDetail.ABSTRACT, db=db, linked_detail=None)
            direct_items.append((formatted, child))
            all_page_ids.append(child.id)
            structural_ids.add(child.id)
        for child, j in child_judgements:
            formatted = (
                f'*On: {child.headline} (`{child.id[:8]}`)*\n'
                + await format_page(j, PageDetail.ABSTRACT, db=db, linked_detail=None)
            )
            direct_items.append((formatted, j))
            all_page_ids.append(j.id)
            structural_ids.add(j.id)

        parts.append('# Direct Context')
        parts.append('')
        parts.append(group_by_credence(direct_items, heading_level='##'))

    headline_lines: list[str] = []
    headline_ids: list[str] = []

    if ancestry:
        ancestor_ids = {a.id for a in ancestry}
        siblings_by_ancestor: dict[str, list[Page]] = {
            a.id: await source.get_child_questions(a.id) for a in ancestry
        }
        judgement_qids: list[str] = [a.id for a in ancestry]
        for sibs in siblings_by_ancestor.values():
            for sib in sibs:
                if sib.id != question_id and sib.id not in ancestor_ids:
                    judgement_qids.append(sib.id)
        judgements_by_qid = await source.get_judgements_for_questions(judgement_qids)

        headline_lines.append('## Ancestry & Siblings')
        headline_lines.append('')
        for ancestor in reversed(ancestry):
            structural_ids.add(ancestor.id)
            headline_lines.append(await format_page(ancestor, PageDetail.HEADLINE))
            a_judgements = judgements_by_qid.get(ancestor.id, [])
            if a_judgements:
                latest = max(a_judgements, key=lambda j: j.created_at)
                headline_lines.append(
                    '  ' + await format_page(latest, PageDetail.HEADLINE)
                )
                headline_ids.append(latest.id)
                structural_ids.add(latest.id)
            siblings = siblings_by_ancestor[ancestor.id]
            for sib in siblings:
                if sib.id == question_id or sib.id in ancestor_ids:
                    continue
                headline_lines.append(
                    '  ' + await format_page(sib, PageDetail.HEADLINE)
                )
                headline_ids.append(sib.id)
                structural_ids.add(sib.id)
                sib_judgements = judgements_by_qid.get(sib.id, [])
                if sib_judgements:
                    latest = max(sib_judgements, key=lambda j: j.created_at)
                    headline_lines.append(
                        '    ' + await format_page(latest, PageDetail.HEADLINE)
                    )
                    headline_ids.append(latest.id)
                    structural_ids.add(latest.id)
        headline_lines.append('')

    subtree_lines, subtree_ids = await _render_subtree_headlines(
        question_id, source, db,
    )
    if subtree_lines:
        headline_lines.append('## Scope Subtree')
        headline_lines.append('')
        headline_lines.extend(subtree_lines)
        headline_lines.append('')
        headline_ids.extend(subtree_ids)
        structural_ids.update(subtree_ids)

    if headline_lines:
        parts.append('# Wider Context (Headlines)')
        parts.append('')
        parts.extend(headline_lines)

    all_page_ids.extend(headline_ids)

    embedding_result = await build_embedding_based_context(
        question.headline,
        db,
        scope_question_id=question_id,
        headline_only_ids=structural_ids,
    )
    if embedding_result.context_text:
        parts.append('# Embedding Search Results')
        parts.append('')
        parts.append(embedding_result.context_text)
        parts.append('')
    all_page_ids.extend(embedding_result.page_ids)

    return ScoutContextResult(
        context_text='\n'.join(parts),
        page_ids=all_page_ids,
        structural_page_ids=structural_ids,
    )


async def render_subtree(
    root_id: str,
    db: DB,
    *,
    graph: PageGraph | None = None,
    detail: PageDetail = PageDetail.CONTENT,
    linked_detail: PageDetail | None = PageDetail.HEADLINE,
    content_page_ids: set[str] | None = None,
) -> str:
    """Render a full subtree of pages starting from a root page.

    Loads all pages and links into a PageGraph (if not already provided)
    to avoid per-page DB round trips, then walks the tree depth-first:
    question -> considerations, judgements, child questions (recurse).

    Non-question root pages are rendered standalone with their outgoing links.

    Pages whose IDs appear in *content_page_ids* are rendered at CONTENT
    detail regardless of the *detail* parameter.
    """
    if graph is None:
        graph = await PageGraph.load(db)

    root = await graph.get_page(root_id)
    if not root:
        return f'[Page {root_id} not found]'

    _content_ids = content_page_ids or set()
    parts: list[str] = []
    visited: set[str] = set()

    async def _render_question(question: Page, depth: int) -> None:
        if question.id in visited:
            parts.append(f'{"  " * depth}(cycle: `{question.id[:8]}`)')
            return
        visited.add(question.id)

        q_detail = PageDetail.CONTENT if question.id in _content_ids else detail
        indent = '  ' * depth
        parts.append(
            indent + await format_page(question, q_detail, linked_detail=None, db=db, graph=graph)
        )

        considerations = await graph.get_considerations_for_question(question.id)
        if considerations:
            con_items: list[tuple[str, Page]] = []
            for claim, link in considerations:
                visited.add(claim.id)
                direction = f'({link.direction.value}) ' if link.direction else ''
                line = (
                    f'{indent}- {direction}'
                    + await format_page(claim, linked_detail or PageDetail.HEADLINE, linked_detail=None, graph=graph)
                )
                if link.reasoning:
                    line += f'\n{indent}  Reasoning: {link.reasoning}'
                con_items.append((line, claim))
            parts.append('')
            hn = min(depth + 3, 6)
            grouped = group_by_credence(con_items, heading_level='#' * hn, separator='\n')
            parts.append(grouped)

        all_judgements = await graph.get_judgements_for_question(question.id)
        judgements = (
            [max(all_judgements, key=lambda j: j.created_at)] if all_judgements else []
        )
        if judgements:
            parts.append('')
            parts.append(f'{indent}**Judgements:**')
            for j in judgements:
                visited.add(j.id)
                parts.append(
                    f'{indent}- '
                    + await format_page(j, linked_detail or PageDetail.HEADLINE, linked_detail=None, graph=graph)
                )

        children = await graph.get_child_questions(question.id)
        if children:
            parts.append('')
            parts.append(f'{indent}**Sub-questions:**')
            parts.append('')
            for child in children:
                await _render_question(child, depth + 1)
                parts.append('')

    if root.page_type == PageType.QUESTION:
        await _render_question(root, 0)
    else:
        parts.append(await format_page(root, detail, linked_detail=linked_detail, graph=graph))

    return '\n'.join(parts)


async def _resolve_superseding_page(
    page: Page,
    db: DB | None,
    graph: PageGraph | None,
) -> Page | None:
    """Resolve the supersession chain to the final active replacement page."""
    if graph is not None:
        result = await graph.resolve_supersession_chain(page)
        if result is not None:
            return result
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
    graph: PageGraph | None = None,
    include_superseding: bool = True,
    exclude_page_ids: set[str] | None = None,
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
        replacement = await _resolve_superseding_page(page, db, graph)
        original = await format_page(
            page, detail, linked_detail=linked_detail,
            db=db, graph=graph, include_superseding=False,
        )
        if replacement:
            replacement_text = await format_page(
                replacement, detail, linked_detail=linked_detail,
                db=db, graph=graph, include_superseding=False,
            )
            if detail == PageDetail.HEADLINE:
                return (
                    f'[SUPERSEDED] {original}\n'
                    f'  -> replaced by: {replacement_text}'
                )
            return (
                f'{original}\n\n'
                '> **SUPERSEDED** — this page has been replaced by'
                f' `{replacement.id[:8]}` ({replacement.headline}).'
                ' Current version:\n\n'
                f'{replacement_text}'
            )
        if detail == PageDetail.HEADLINE:
            return f'[SUPERSEDED] {original}'
        return (
            f'{original}\n\n'
            '> **SUPERSEDED** — this page has been replaced'
            ' (replacement not found).'
        )

    if detail != PageDetail.HEADLINE and not page.content and db:
        full = await db.get_page(page.id)
        if full:
            page = full

    if detail == PageDetail.HEADLINE:
        tag = f'{page.page_type.value.upper()}'
        if page.credence is not None:
            tag += f' C{page.credence}/R{page.robustness}'
        return f'[{tag}] `{page.id[:8]}` -- {page.headline}'

    extra = page.extra or {}
    lines = [
        f"### [{page.page_type.value.upper()}] {page.headline}",
        f"ID: {page.id}",
    ]
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

    source: DB | PageGraph | None = graph if graph is not None else db
    _exclude = exclude_page_ids or set()
    if linked_detail is not None and source and page.page_type == PageType.QUESTION:
        linked_items: list[tuple[str, Page]] = []

        considerations = await source.get_considerations_for_question(page.id)
        for claim, link in considerations:
            if claim.id in _exclude:
                continue
            line = "- " + await format_page(claim, linked_detail, db=db, graph=graph, linked_detail=None)
            if link.reasoning:
                line += f"\n  Reasoning: {link.reasoning}"
            linked_items.append((line, claim))

        judgements = await source.get_judgements_for_question(page.id)
        for j in judgements:
            if j.id in _exclude:
                continue
            line = '- ' + await format_page(j, linked_detail, db=db, graph=graph, linked_detail=None)
            linked_items.append((line, j))

        children = await source.get_child_questions(page.id)
        child_judgements = await source.get_judgements_for_questions(
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
                    + await format_page(j, linked_detail, db=db, graph=graph, linked_detail=None)
                )
                linked_items.append((line, j))

        if linked_items:
            lines.append("")
            lines.append(group_by_credence(linked_items, heading_level="####", separator="\n"))

    return "\n".join(lines)


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
    question = await source.get_page(question_id)
    if not question:
        return f"[Question {question_id} not found]", short_id_map, []

    working_context = await format_page(
        question, PageDetail.ABSTRACT, db=db, graph=graph,
    )
    working_page_ids = [question_id]

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
                parts.append(await format_page(page, PageDetail.CONTENT, db=db, graph=graph))

    context_text = "\n".join(parts)
    log.debug(
        "build_call_context complete: %d chars, %d working pages, %d extra pages",
        len(context_text), len(working_page_ids), len(extra_page_ids or []),
    )
    return context_text, short_id_map, working_page_ids


async def format_question_for_find_considerations(
    question_id: str,
    db: DB,
    graph: PageGraph | None = None,
) -> tuple[str, list[str]]:
    """Build find-considerations working context with role-aware display.

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
    parts.append(await format_page(question, PageDetail.HEADLINE))
    parts.append("")

    considerations = await source.get_considerations_for_question(question_id)
    direct_cons = [(p, l) for p, l in considerations if l.role == LinkRole.DIRECT]
    structural_cons = [(p, l) for p, l in considerations if l.role == LinkRole.STRUCTURAL]

    children_with_links = await source.get_child_questions_with_links(question_id)
    direct_children = [(p, l) for p, l in children_with_links if l.role == LinkRole.DIRECT]
    structural_children = [(p, l) for p, l in children_with_links if l.role == LinkRole.STRUCTURAL]

    all_con_items: list[tuple[str, Page]] = []

    for claim, link in direct_cons:
        loaded_ids.append(claim.id)
        cr = f" C{claim.credence}/R{claim.robustness}" if claim.credence is not None else ""
        all_con_items.append((f"-{cr} {claim.headline}", claim))
    for child, _link in direct_children:
        loaded_ids.append(child.id)
        all_con_items.append((f"- [sub-Q] {child.headline}", child))

    for claim, link in structural_cons:
        loaded_ids.append(claim.id)
        if not claim.content:
            full = await db.get_page(claim.id)
            if full:
                claim = full
        lines_buf = [f"### [{claim.page_type.value.upper()}] {claim.headline}"]
        lines_buf.append(f"ID: {claim.id}")
        if claim.credence is not None:
            lines_buf.append(f"Credence: {claim.credence}/9 | Robustness: {claim.robustness}/5")
        lines_buf.append("")
        lines_buf.append(claim.content)
        all_con_items.append(("\n".join(lines_buf), claim))
    for child, _link in structural_children:
        loaded_ids.append(child.id)
        if not child.content:
            full = await db.get_page(child.id)
            if full:
                child = full
        lines_buf = [f"### [QUESTION] {child.headline}", f"ID: {child.id}", "", child.content]
        all_con_items.append(("\n".join(lines_buf), child))

    judgements = await source.get_judgements_for_question(question_id)
    for j in judgements:
        loaded_ids.append(j.id)
        all_con_items.append((await format_page(j, PageDetail.HEADLINE), j))

    children = await source.get_child_questions(question_id)
    child_judgements = await source.get_judgements_for_questions(
        [c.id for c in children]
    )
    for child in children:
        for j in child_judgements.get(child.id, []):
            loaded_ids.append(j.id)
            line = (
                f"*On sub-question: {child.headline} (`{child.id}`)*\n"
                + await format_page(j, PageDetail.HEADLINE)
            )
            all_con_items.append((line, j))

    if all_con_items:
        parts.append(group_by_credence(all_con_items, heading_level="##"))
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
    Includes consideration count, last find-considerations fruit/date, and hypothesis flag."""
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
    fc_info = await db.get_last_find_considerations_info(question_id)
    if fc_info:
        date_str = fc_info[0][:10]
        fruit = fc_info[1]
        fruit_str = f"fruit={fruit}" if fruit is not None else "fruit=?"
        fc_str = f"{fruit_str} · {date_str}"
    else:
        fc_str = "never explored"

    lines = [
        f"{prefix}{tag}{hypothesis_tag} `{question_id}` — {question.headline} "
        f"({n_cons} cons · {fc_str})"
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


async def _build_dependency_signal(db: DB) -> str | None:
    """Build a section listing the most-depended-on pages in the workspace.

    Returns None if no DEPENDS_ON links exist yet.
    """
    counts = await db.get_dependency_counts()
    if not counts:
        return None

    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]
    pages = await db.get_pages_by_ids([pid for pid, _ in top])
    lines = ['## Load-Bearing Pages (by dependency count)', '']
    lines.append(
        'These pages are depended on by the most other pages. '
        'Prioritize them for robustness assessment — if they turn out to '
        'be wrong, the most downstream conclusions would be affected.'
    )
    lines.append('')
    for pid, count in top:
        page = pages.get(pid)
        if page:
            stale_tag = ' [SUPERSEDED]' if page.is_superseded else ''
            lines.append(
                f'- `{pid[:8]}` — {page.headline} '
                f'({count} dependents){stale_tag}'
            )
    return '\n'.join(lines)


async def build_prioritization_context(
    db: DB,
    scope_question_id: str | None = None,
    graph: PageGraph | None = None,
) -> tuple[str, dict[str, str]]:
    """Build context for a prioritization call.

    Uses embedding-similarity search to surface the most relevant pages
    from the workspace, then appends the scope question's full subtree
    (at ABSTRACT detail) and a dispatchable question index.

    Returns (context_text, short_id_map) where short_id_map maps 8-char
    short IDs to full UUIDs.
    """
    source: DB | PageGraph = graph if graph is not None else db
    parts: list[str] = ['# Prioritization Context', '']
    short_id_map: dict[str, str] = {}

    if scope_question_id:
        question = await source.get_page(scope_question_id)
        if question:
            subtree_page_ids = await collect_all_subtree_page_ids(
                scope_question_id, db, graph=graph,
            )
            embedding_result = await build_embedding_based_context(
                question.headline,
                db,
                scope_question_id=scope_question_id,
                headline_only_ids=subtree_page_ids,
            )
            if embedding_result.context_text:
                parts.append(embedding_result.context_text)
                parts.append('')
                parts.append('---')
                parts.append('')

            index_lines = await _build_question_index(
                scope_question_id, db, graph=graph,
            )
            parts.append('## Scope Subtree — Dispatchable Questions')
            parts.append('')
            parts.append(
                'You can only dispatch research calls on questions in this subtree '
                '(or on new subquestions you create during this call). '
                'Use only these exact IDs in your dispatch tags:'
            )
            parts.append('')
            parts.extend(index_lines)
            parts.append('')

            direct_children = await source.get_child_questions(scope_question_id)
            full_page_ids = {scope_question_id} | {c.id for c in direct_children}
            subtree_text = await render_subtree(
                scope_question_id, db,
                graph=graph,
                detail=PageDetail.ABSTRACT,
                linked_detail=PageDetail.ABSTRACT,
                content_page_ids=full_page_ids,
            )
            parts.append('## Scope Subtree — Detail')
            parts.append('')
            parts.append(subtree_text)
            parts.append('')

            subtree_ids = await collect_subtree_ids(
                scope_question_id, db, graph=graph,
            )
            for sid in subtree_ids:
                short_id_map[sid[:8]] = sid

    dep_section = await _build_dependency_signal(db)
    if dep_section:
        parts.append(dep_section)
        parts.append('')

    return '\n'.join(parts), short_id_map



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
        field_name='abstract',
    )

    _exclude = exclude_page_ids or set()
    if _exclude:
        ranked = [(p, s) for p, s in ranked if p.id not in _exclude]

    scope_section = ''
    scope_page_ids: list[str] = []
    if scope_question_id:
        scope_page = await db.get_page(scope_question_id)
        if scope_page:
            scope_section = (
                '## Scope Question\n\n'
                + await format_page(
                    scope_page,
                    scope_detail or PageDetail.ABSTRACT,
                    linked_detail=scope_linked_detail or PageDetail.HEADLINE,
                    db=db,
                    exclude_page_ids=_exclude,
                )
                + '\n\n'
            )
            scope_page_ids = [scope_question_id]
            ranked = [(p, s) for p, s in ranked if p.id != scope_question_id]

    if require_judgement_for_questions:
        question_ids = [p.id for p, _ in ranked if p.page_type == PageType.QUESTION]
        judgements_by_qid = await db.get_judgements_for_questions(question_ids)
        has_judgement = {qid for qid, js in judgements_by_qid.items() if js}
        ranked = [
            (p, s) for p, s in ranked
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
        formatted = await format_page(page, PageDetail.CONTENT, db=db, linked_detail=None)
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
            formatted = await format_page(page, PageDetail.CONTENT, db=db, linked_detail=None)
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
        sections.append(group_by_credence(all_items, heading_level='##'))

    context_text = '\n'.join(sections)

    budget_usage = {
        'distillation': distillation_chars,
        'full': full_chars,
        'abstract': abstract_chars,
        'summary': summary_chars,
    }
    log.info(
        'Embedding context: full=%d/%d chars, abstract=%d/%d chars, '
        'summary=%d/%d chars, distillation=%d/%d chars, '
        'pages=%d full + %d abstract + %d summary',
        full_chars, full_budget, abstract_chars, abstract_budget,
        summary_chars, summary_budget,
        distillation_chars, distillation_budget,
        len(full_ids), len(abstract_ids), len(summary_ids),
    )

    all_ids = (
        scope_page_ids + distillation_ids + full_ids
        + abstract_ids + summary_ids
    )
    return EmbeddingBasedContextResult(
        context_text=context_text,
        page_ids=all_ids,
        full_page_ids=full_ids,
        abstract_page_ids=abstract_ids,
        summary_page_ids=summary_ids,
        distillation_page_ids=distillation_ids,
        budget_usage=budget_usage,
    )
