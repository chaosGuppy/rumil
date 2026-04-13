"""
View construction for research questions.

Builds structured representations of research state from the page graph,
usable for both LLM context injection and frontend rendering.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from rumil.database import DB
from rumil.models import (
    CallType,
    ConsiderationDirection,
    LinkRole,
    LinkType,
    Page,
    PageLink,
    PageType,
)


@dataclass
class ViewItem:
    page: Page
    links: list[PageLink]
    section: str

    @property
    def sort_key(self) -> tuple[int, int, int]:
        """Lower = more important. Used for budget-constrained rendering."""
        importance = self.page.importance if self.page.importance is not None else 99
        inv_credence = 10 - (self.page.credence or 5)
        inv_robustness = 6 - (self.page.robustness or 3)
        return (importance, inv_credence, inv_robustness)


@dataclass
class ViewSection:
    name: str
    description: str
    items: list[ViewItem] = field(default_factory=list)


@dataclass
class ViewHealth:
    total_pages: int
    missing_credence: int
    missing_importance: int
    child_questions_without_judgements: int
    max_depth: int


@dataclass
class View:
    question: Page
    sections: list[ViewSection]
    health: ViewHealth


SECTION_DEFS: list[tuple[str, str]] = [
    ("current_position", "Active judgement on the question"),
    ("core_findings", "Considerations with importance 0-1 and credence >= 6"),
    ("live_hypotheses", "Considerations still being tested or uncertain"),
    ("key_evidence", "Considerations backed by sources or produced by ingestion"),
    ("key_uncertainties", "Unresolved child questions or balanced considerations"),
    ("structural_framing", "Structural considerations and sub-questions"),
    ("supporting_detail", "Lower-importance material within the threshold"),
    ("promotion_candidates", "High-credence pages that may deserve higher importance"),
    ("demotion_candidates", "Core pages with low robustness"),
]


def _section_order(name: str) -> int:
    for i, (n, _) in enumerate(SECTION_DEFS):
        if n == name:
            return i
    return len(SECTION_DEFS)


_INGEST_CALL_TYPES = {
    CallType.INGEST.value,
    CallType.WEB_RESEARCH.value,
}


async def build_view(
    db: DB,
    question_id: str,
    *,
    importance_threshold: int = 3,
) -> View:
    """Build a structured view of research on a question."""
    question = await db.get_page(question_id)
    if not question:
        raise ValueError(f"Question {question_id} not found")
    if question.page_type != PageType.QUESTION:
        raise ValueError(
            f"Page {question_id[:8]} is {question.page_type.value}, not a question"
        )

    considerations, child_questions_with_links, judgements = await _fetch_direct(
        db, question_id
    )

    child_question_ids = [p.id for p, _ in child_questions_with_links]
    child_judgements_by_q = await db.get_judgements_for_questions(child_question_ids)

    consideration_page_ids = [p.id for p, _ in considerations]
    outgoing_by_page = await db.get_links_from_many(consideration_page_ids)

    cites_page_ids: set[str] = set()
    for pid, links in outgoing_by_page.items():
        for link in links:
            if link.link_type == LinkType.CITES:
                cites_page_ids.add(pid)

    max_depth = await _measure_child_depth(db, question_id)

    sections_dict: dict[str, ViewSection] = {}
    for name, desc in SECTION_DEFS:
        sections_dict[name] = ViewSection(name=name, description=desc)

    seen_pages: dict[str, ViewItem] = {}
    all_scored_pages: list[Page] = []

    active_judgements = [j for j in judgements if j.is_active()]
    if active_judgements:
        latest = max(active_judgements, key=lambda j: j.created_at)
        judgement_links = [
            link
            for p, link in await _get_answers_links(db, question_id)
            if p.id == latest.id
        ]
        item = ViewItem(page=latest, links=judgement_links, section="current_position")
        sections_dict["current_position"].items.append(item)
        seen_pages[latest.id] = item
        all_scored_pages.append(latest)

    for claim, link in considerations:
        if not claim.is_active():
            continue
        all_scored_pages.append(claim)
        assigned = _classify_consideration(
            claim,
            link,
            has_cites=claim.id in cites_page_ids,
            importance_threshold=importance_threshold,
        )
        for section_name in assigned:
            if section_name not in sections_dict:
                continue
            item = ViewItem(page=claim, links=[link], section=section_name)
            sections_dict[section_name].items.append(item)
            if claim.id not in seen_pages:
                seen_pages[claim.id] = item

    for child, link in child_questions_with_links:
        if not child.is_active():
            continue
        all_scored_pages.append(child)
        child_js = child_judgements_by_q.get(child.id, [])
        assigned = _classify_child_question(
            child, link, child_js, importance_threshold=importance_threshold
        )
        for section_name in assigned:
            if section_name not in sections_dict:
                continue
            item = ViewItem(page=child, links=[link], section=section_name)
            sections_dict[section_name].items.append(item)
            if child.id not in seen_pages:
                seen_pages[child.id] = item

    sections = [s for s in sections_dict.values() if s.items]
    sections.sort(key=lambda s: _section_order(s.name))
    for section in sections:
        section.items.sort(key=lambda item: item.sort_key)

    health = _compute_health(
        all_scored_pages,
        child_questions_with_links,
        child_judgements_by_q,
        max_depth,
    )

    return View(question=question, sections=sections, health=health)


async def _fetch_direct(
    db: DB, question_id: str
) -> tuple[
    list[tuple[Page, PageLink]],
    list[tuple[Page, PageLink]],
    list[Page],
]:
    """Fetch considerations, child questions with links, and judgements for a question."""
    considerations = await db.get_considerations_for_question(question_id)
    child_questions_with_links = await db.get_child_questions_with_links(question_id)
    judgements = await db.get_judgements_for_question(question_id)
    return considerations, child_questions_with_links, judgements


async def _get_answers_links(db: DB, question_id: str) -> list[tuple[Page, PageLink]]:
    """Get (judgement_page, link) pairs for ANSWERS links on a question."""
    links = await db.get_links_to(question_id)
    answers_links = [lk for lk in links if lk.link_type == LinkType.ANSWERS]
    if not answers_links:
        return []
    pages = await db.get_pages_by_ids([lk.from_page_id for lk in answers_links])
    return [
        (pages[lk.from_page_id], lk) for lk in answers_links if lk.from_page_id in pages
    ]


def _classify_consideration(
    claim: Page,
    link: PageLink,
    *,
    has_cites: bool,
    importance_threshold: int,
) -> list[str]:
    """Return section names a consideration belongs to. First is primary."""
    sections: list[str] = []
    imp = claim.importance
    cred = claim.credence
    rob = claim.robustness

    if link.role == LinkRole.STRUCTURAL:
        sections.append("structural_framing")

    if imp is not None and imp <= 1 and cred is not None and cred >= 6:
        sections.append("core_findings")

    if (
        imp is not None
        and imp <= 2
        and ((rob is not None and rob <= 2) or (cred is not None and 4 <= cred <= 6))
    ):
        sections.append("live_hypotheses")

    if has_cites or claim.provenance_call_type in _INGEST_CALL_TYPES:
        sections.append("key_evidence")

    if cred is not None and 4 <= cred <= 6 and rob is not None and rob >= 3:
        sections.append("key_uncertainties")

    if (
        imp is not None
        and imp >= 2
        and cred is not None
        and cred >= 7
        and rob is not None
        and rob >= 3
    ):
        sections.append("promotion_candidates")

    if imp is not None and imp == 0 and rob is not None and rob <= 2:
        sections.append("demotion_candidates")

    if not sections and imp is not None and imp <= importance_threshold:
        sections.append("supporting_detail")

    if not sections and (imp is None or imp <= importance_threshold):
        sections.append("supporting_detail")

    return sections


def _classify_child_question(
    child: Page,
    link: PageLink,
    judgements: Sequence[Page],
    *,
    importance_threshold: int,
) -> list[str]:
    """Return section names a child question belongs to."""
    sections: list[str] = []

    if link.role == LinkRole.STRUCTURAL:
        sections.append("structural_framing")

    active_judgements = [j for j in judgements if j.is_active()]
    if not active_judgements:
        sections.append("key_uncertainties")

    if not sections:
        imp = child.importance
        if imp is not None and imp <= importance_threshold:
            sections.append("supporting_detail")
        elif imp is None:
            sections.append("supporting_detail")

    return sections


async def _measure_child_depth(
    db: DB,
    question_id: str,
    *,
    max_depth: int = 20,
) -> int:
    """BFS to measure maximum child question nesting depth. Cycle-safe."""
    visited: set[str] = {question_id}
    frontier = [question_id]
    depth = 0

    for level in range(max_depth):
        if not frontier:
            break
        links_by_parent = await db.get_links_from_many(frontier)
        next_frontier: list[str] = []
        for parent_id in frontier:
            for link in links_by_parent.get(parent_id, []):
                if (
                    link.link_type == LinkType.CHILD_QUESTION
                    and link.to_page_id not in visited
                ):
                    visited.add(link.to_page_id)
                    next_frontier.append(link.to_page_id)
        if next_frontier:
            depth = level + 1
        frontier = next_frontier

    return depth


def _compute_health(
    all_pages: Sequence[Page],
    child_questions_with_links: Sequence[tuple[Page, PageLink]],
    child_judgements_by_q: dict[str, list[Page]],
    max_depth: int,
) -> ViewHealth:
    missing_credence = sum(
        1
        for p in all_pages
        if p.page_type in (PageType.CLAIM, PageType.JUDGEMENT) and p.credence is None
    )
    missing_importance = sum(1 for p in all_pages if p.importance is None)
    child_without_judgements = sum(
        1
        for child, _ in child_questions_with_links
        if child.is_active()
        and not any(j.is_active() for j in child_judgements_by_q.get(child.id, []))
    )
    return ViewHealth(
        total_pages=len(all_pages),
        missing_credence=missing_credence,
        missing_importance=missing_importance,
        child_questions_without_judgements=child_without_judgements,
        max_depth=max_depth,
    )


def render_view_as_context(view: View, *, char_budget: int = 8000) -> str:
    """Render a view as markdown for LLM context injection."""
    parts: list[str] = []
    parts.append(f"# {view.question.headline}")
    parts.append(f"ID: {view.question.id}")
    parts.append("")

    if view.health.total_pages == 0:
        parts.append("*No research yet.*")
        return "\n".join(parts)

    rendered_items: list[tuple[int, str, ViewItem]] = []
    for section in view.sections:
        for item in section.items:
            text = _render_item(item)
            priority = _section_order(item.section)
            rendered_items.append((priority, text, item))

    rendered_items.sort(key=lambda t: (t[0], t[2].sort_key))

    seen_page_ids: set[str] = set()
    budget_remaining = char_budget - len("\n".join(parts)) - 200
    current_section: str | None = None
    body_parts: list[str] = []

    for priority, text, item in rendered_items:
        if item.page.id in seen_page_ids:
            continue

        section_header = ""
        if item.section != current_section:
            section_name = item.section.replace("_", " ").title()
            section_obj = next(
                (s for s in view.sections if s.name == item.section), None
            )
            desc = section_obj.description if section_obj else ""
            section_header = f"\n## {section_name}\n{desc}\n\n"

        entry_cost = len(section_header) + len(text) + 1
        if entry_cost > budget_remaining:
            if budget_remaining < 100:
                break
            continue

        if item.section != current_section:
            body_parts.append(section_header)
            current_section = item.section

        body_parts.append(text)
        seen_page_ids.add(item.page.id)
        budget_remaining -= entry_cost

    if body_parts:
        parts.extend(body_parts)

    parts.append("")
    parts.append(_render_health(view.health))

    return "\n".join(parts)


def _render_item(item: ViewItem) -> str:
    page = item.page
    tag = page.page_type.value.upper()
    scores: list[str] = []
    if page.credence is not None:
        scores.append(f"C{page.credence}")
    if page.robustness is not None:
        scores.append(f"R{page.robustness}")
    if page.importance is not None:
        scores.append(f"L{page.importance}")
    score_str = " " + "/".join(scores) if scores else ""

    direction_str = ""
    for link in item.links:
        if link.direction and link.direction != ConsiderationDirection.NEUTRAL:
            direction_str = f" ({link.direction.value})"
            break

    headline = page.headline
    short_id = page.id[:8]

    line = f"- [{tag}{score_str}] `{short_id}`{direction_str} {headline}"

    if page.abstract and len(page.abstract) < 200:
        line += f"\n  {page.abstract}"

    return line


def _render_health(health: ViewHealth) -> str:
    lines = ["---", "**Research health:**"]
    lines.append(f"- {health.total_pages} pages")
    if health.missing_credence:
        lines.append(f"- {health.missing_credence} claims/judgements missing credence")
    if health.missing_importance:
        lines.append(f"- {health.missing_importance} pages missing importance")
    if health.child_questions_without_judgements:
        lines.append(
            f"- {health.child_questions_without_judgements}"
            " child questions without judgements"
        )
    lines.append(f"- Max sub-question depth: {health.max_depth}")
    return "\n".join(lines)
