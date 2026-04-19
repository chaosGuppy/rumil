"""Export a rumil project as an Obsidian vault.

Each active page becomes a markdown file with YAML frontmatter and
Obsidian-style ``[[wiki links]]`` replacing the internal ``[shortid]``
references. Structural links (considerations, child questions,
dependencies, etc.) are rendered as a Links section at the bottom
of each file.
"""

import logging
import re
from collections import defaultdict
from pathlib import Path

from rumil.database import DB
from rumil.models import (
    ConsiderationDirection,
    LinkType,
    Page,
    PageLink,
)

log = logging.getLogger(__name__)

_CITATION_RE = re.compile(r"\[([a-f0-9]{8})\]")

_LINK_TYPE_LABELS: dict[LinkType, str] = {
    LinkType.CONSIDERATION: "Consideration",
    LinkType.CHILD_QUESTION: "Sub-question",
    LinkType.SUPERSEDES: "Supersedes",
    LinkType.RELATED: "Related",
    LinkType.VARIANT: "Variant",
    LinkType.SUMMARIZES: "Summarizes",
    LinkType.CITES: "Cites",
    LinkType.DEPENDS_ON: "Depends on",
    LinkType.ANSWERS: "Answers",
    LinkType.VIEW_OF: "View of",
}


def _slugify(text: str, max_len: int = 80) -> str:
    """Turn a headline into a filesystem-safe slug."""
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s-]+", "-", slug).strip("-")
    return slug[:max_len].rstrip("-")


def _make_filename(page: Page, seen: dict[str, int]) -> str:
    """Generate a unique human-readable filename (without extension).

    Format: ``{type}-{slug}`` with a numeric suffix if the slug
    collides with an earlier page.
    """
    slug = _slugify(page.headline)
    if not slug:
        slug = page.id[:8]
    base = f"{page.page_type.value}-{slug}"
    count = seen.get(base, 0)
    seen[base] = count + 1
    if count > 0:
        return f"{base}-{count}"
    return base


def _build_name_maps(
    pages: list[Page],
) -> tuple[dict[str, str], dict[str, str]]:
    """Build two maps from a list of pages.

    Returns (id_to_name, short_to_name) where:
    - id_to_name maps full UUID to the human-readable filename
    - short_to_name maps the 8-char short ID to the same filename
    """
    seen: dict[str, int] = {}
    id_to_name: dict[str, str] = {}
    short_to_name: dict[str, str] = {}
    for page in pages:
        name = _make_filename(page, seen)
        id_to_name[page.id] = name
        short_to_name[page.id[:8]] = name
    return id_to_name, short_to_name


def _rewrite_citations(content: str, short_to_name: dict[str, str]) -> str:
    """Replace ``[shortid]`` citations with ``[[Wiki Name]]`` links."""

    def _replace(match: re.Match[str]) -> str:
        short_id = match.group(1)
        name = short_to_name.get(short_id)
        if name:
            return f"[[{name}]]"
        return match.group(0)

    return _CITATION_RE.sub(_replace, content)


def _direction_label(direction: ConsiderationDirection | None) -> str:
    if direction == ConsiderationDirection.SUPPORTS:
        return " (supports)"
    if direction == ConsiderationDirection.OPPOSES:
        return " (opposes)"
    return ""


def _render_links_section(
    page_id: str,
    links: list[PageLink],
    id_to_name: dict[str, str],
) -> str:
    """Render a Links section with wiki links grouped by relationship."""
    outgoing: list[tuple[str, str]] = []
    incoming: list[tuple[str, str]] = []

    for link in links:
        label = _LINK_TYPE_LABELS.get(link.link_type, link.link_type.value)
        if link.from_page_id == page_id:
            target = id_to_name.get(link.to_page_id)
            if target:
                dir_label = _direction_label(link.direction)
                outgoing.append((f"{label}{dir_label}", target))
        else:
            source = id_to_name.get(link.from_page_id)
            if source:
                dir_label = _direction_label(link.direction)
                incoming.append((f"{label}{dir_label}", source))

    if not outgoing and not incoming:
        return ""

    lines = ["", "---", "", "## Links", ""]
    if outgoing:
        for label, target in outgoing:
            lines.append(f"- {label}: [[{target}]]")
    if incoming:
        if outgoing:
            lines.append("")
        for label, source in incoming:
            lines.append(f"- {label} (from [[{source}]])")

    return "\n".join(lines)


def _render_page(
    page: Page,
    id_to_name: dict[str, str],
    short_to_name: dict[str, str],
    links_for_page: list[PageLink],
) -> str:
    """Render a single page as Obsidian-compatible markdown."""
    frontmatter_fields = [
        f"id: {page.id}",
        f"type: {page.page_type.value}",
        f"layer: {page.layer.value}",
    ]
    if page.credence is not None:
        frontmatter_fields.append(f"credence: {page.credence}")
    if page.robustness is not None:
        frontmatter_fields.append(f"robustness: {page.robustness}")
    if page.fruit_remaining is not None:
        frontmatter_fields.append(f"fruit_remaining: {page.fruit_remaining}")
    frontmatter_fields.append(
        f"created: {page.created_at.strftime('%Y-%m-%dT%H:%M:%S')}"
    )
    if page.provenance_call_type:
        frontmatter_fields.append(f"provenance: {page.provenance_call_type}")

    frontmatter = "---\n" + "\n".join(frontmatter_fields) + "\n---\n"

    content = _rewrite_citations(page.content, short_to_name)

    links_section = _render_links_section(page.id, links_for_page, id_to_name)

    return f"{frontmatter}\n# {page.headline}\n\n{content}{links_section}\n"


async def _collect_subtree_page_ids(
    db: DB,
    root_question_id: str,
) -> set[str]:
    """BFS from a root question, collecting all descendant question IDs
    and all pages linked to any question in the subtree.

    Returns the full set of page IDs to export (questions + their
    connected claims, judgements, sources, etc.).
    """
    question_ids: set[str] = {root_question_id}
    frontier = [root_question_id]

    while frontier:
        children = []
        for qid in frontier:
            child_pages = await db.get_child_questions(qid)
            for child in child_pages:
                if child.id not in question_ids:
                    question_ids.add(child.id)
                    children.append(child.id)
        frontier = children

    # First pass: links touching any question → picks up claims,
    # judgements, child questions, views, etc.
    all_ids = set(question_ids)
    first_links = await db.get_all_links(page_ids=question_ids)
    for link in first_links:
        all_ids.add(link.from_page_id)
        all_ids.add(link.to_page_id)

    # Second pass: links touching the non-question pages we just
    # discovered → picks up sources (via CITES from claims),
    # variants, depends_on targets, etc.
    new_ids = all_ids - question_ids
    if new_ids:
        second_links = await db.get_all_links(page_ids=new_ids)
        for link in second_links:
            all_ids.add(link.from_page_id)
            all_ids.add(link.to_page_id)

    summaries = await db.get_latest_summaries_for_questions(list(question_ids))
    for summary in summaries.values():
        if summary:
            all_ids.add(summary.id)

    return all_ids


async def export_obsidian(
    db: DB,
    output_dir: str,
    question_id: str | None = None,
    summary_text: str | None = None,
) -> Path:
    """Export pages as an Obsidian vault.

    When *question_id* is given, exports only the question's subtree:
    the question, all its descendant questions, and every page linked
    to any of those questions (claims, judgements, sources, etc.).

    When *question_id* is None, exports all active pages in the project.

    When *summary_text* is provided (e.g. from ``--summary``), it is
    written as a standalone ``executive-summary.md`` file in the vault.

    Returns the output directory path.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    if question_id:
        target_ids = await _collect_subtree_page_ids(db, question_id)
        pages_by_id = await db.get_pages_by_ids(list(target_ids))
        pages = [p for p in pages_by_id.values() if p.is_active()]
    else:
        pages = await db.get_pages(active_only=True)

    if not pages:
        log.warning("No active pages to export")
        return out

    page_ids = {p.id for p in pages}
    all_links = await db.get_all_links(page_ids=page_ids)

    links_by_page: dict[str, list[PageLink]] = defaultdict(list)
    for link in all_links:
        if link.from_page_id in page_ids:
            links_by_page[link.from_page_id].append(link)
        if link.to_page_id in page_ids:
            links_by_page[link.to_page_id].append(link)

    id_to_name, short_to_name = _build_name_maps(pages)

    written = 0
    for page in pages:
        name = id_to_name[page.id]
        filepath = out / f"{name}.md"
        content = _render_page(
            page,
            id_to_name,
            short_to_name,
            links_by_page.get(page.id, []),
        )
        filepath.write_text(content, encoding="utf-8")
        written += 1

    if summary_text:
        summary_path = out / "executive-summary.md"
        summary_path.write_text(
            "---\ntype: executive-summary\n---\n\n"
            f"# Executive Summary\n\n{summary_text}\n",
            encoding="utf-8",
        )
        written += 1

    log.info("Exported %d pages to %s", written, out)
    return out
