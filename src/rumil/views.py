"""
View construction for research questions.

Builds a structured View of research on a question as a function of
workspace state: graph-derived items (considerations, child questions,
judgements) merged with stored VIEW_ITEM pages from update_view when
available. Always works — no update_view pass required — but honors
curation where it exists.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from rumil.constants import DEFAULT_VIEW_SECTIONS
from rumil.database import DB
from rumil.models import (
    CallType,
    LinkRole,
    LinkType,
    Page,
    PageLink,
    PageType,
)

ViewSource = str  # "graph" | "view_item"


@dataclass
class ViewItem:
    page: Page
    links: list[PageLink]
    section: str
    source: ViewSource = "graph"

    @property
    def effective_importance(self) -> int | None:
        """Integer 1-5 importance for ranking/filtering.

        VIEW_ITEM pages: from the VIEW_ITEM link (set by update_view).
        Judgements: implicit 5 (they're the question's current answer).
        Considerations: from link.strength (0-5 float).
        Child questions: from link.impact_on_parent_question (0-10 → 1-5).
        """
        for link in self.links:
            if link.link_type == LinkType.VIEW_ITEM and link.importance is not None:
                return link.importance
        if self.page.page_type == PageType.JUDGEMENT:
            return 5
        for link in self.links:
            if link.link_type == LinkType.CONSIDERATION:
                return max(1, min(5, round(link.strength)))
            if link.link_type == LinkType.CHILD_QUESTION:
                if link.impact_on_parent_question is not None:
                    return max(1, min(5, round(link.impact_on_parent_question / 2)))
                return None
        return None

    @property
    def sort_key(self) -> tuple[int, int, int, int]:
        """Lower tuple = shown first. Curated beats derived at same importance."""
        imp = self.effective_importance
        inv_importance = -(imp if imp is not None else -99)
        source_priority = 0 if self.source == "view_item" else 1
        inv_credence = 10 - (self.page.credence or 5)
        inv_robustness = 6 - (self.page.robustness or 3)
        return (inv_importance, source_priority, inv_credence, inv_robustness)


@dataclass
class ViewSection:
    name: str
    items: list[ViewItem] = field(default_factory=list)


@dataclass
class ViewHealth:
    total_pages: int
    missing_credence: int
    child_questions_without_judgements: int
    max_depth: int


@dataclass
class View:
    question: Page
    sections: list[ViewSection]
    health: ViewHealth
    stored_view: Page | None = None


_SECTION_ORDER = {name: i for i, name in enumerate(DEFAULT_VIEW_SECTIONS)}


def _section_order(name: str) -> int:
    return _SECTION_ORDER.get(name, len(DEFAULT_VIEW_SECTIONS))


_INGEST_CALL_TYPES = {
    CallType.INGEST.value,
    CallType.WEB_RESEARCH.value,
}


async def build_view(db: DB, question_id: str) -> View:
    """Build a structured View of research on a question.

    Merges graph-derived items with stored VIEW_ITEMs when available.
    Graph items that are cited by a VIEW_ITEM are hidden in favour of
    the curated version.
    """
    question = await db.get_page(question_id)
    if not question:
        raise ValueError(f"Question {question_id} not found")
    if question.page_type != PageType.QUESTION:
        raise ValueError(f"Page {question_id[:8]} is {question.page_type.value}, not a question")

    considerations, child_questions_with_links, judgements = await _fetch_direct(db, question_id)

    child_question_ids = [p.id for p, _ in child_questions_with_links]
    child_judgements_by_q = await db.get_judgements_for_questions(child_question_ids)

    consideration_page_ids = [p.id for p, _ in considerations]
    outgoing_by_page = await db.get_links_from_many(consideration_page_ids)
    cites_page_ids = {
        pid
        for pid, links in outgoing_by_page.items()
        if any(l.link_type == LinkType.CITES for l in links)
    }

    stored_view = await db.get_view_for_question(question_id)
    stored_items_with_links: list[tuple[Page, PageLink]] = []
    cited_by_view_items: set[str] = set()
    if stored_view:
        stored_items_with_links = await db.get_view_items(stored_view.id)
        view_item_page_ids = [p.id for p, _ in stored_items_with_links]
        view_item_outgoing = await db.get_links_from_many(view_item_page_ids)
        for links in view_item_outgoing.values():
            for link in links:
                if link.link_type in (LinkType.CITES, LinkType.DEPENDS_ON):
                    cited_by_view_items.add(link.to_page_id)

    max_depth = await _measure_child_depth(db, question_id)

    sections_dict: dict[str, ViewSection] = {
        name: ViewSection(name=name) for name in DEFAULT_VIEW_SECTIONS
    }

    all_scored_pages: list[Page] = []

    active_judgements = [j for j in judgements if j.is_active()]
    if active_judgements:
        latest = max(active_judgements, key=lambda j: j.created_at)
        judgement_links = [
            link for p, link in await _get_answers_links(db, question_id) if p.id == latest.id
        ]
        sections_dict["assessments"].items.append(
            ViewItem(
                page=latest,
                links=judgement_links,
                section="assessments",
                source="graph",
            )
        )
        all_scored_pages.append(latest)

    for claim, link in considerations:
        if not claim.is_active():
            continue
        all_scored_pages.append(claim)
        if claim.id in cited_by_view_items:
            continue
        section = _classify_consideration(
            claim,
            link,
            has_cites=claim.id in cites_page_ids,
        )
        sections_dict[section].items.append(
            ViewItem(page=claim, links=[link], section=section, source="graph")
        )

    for child, link in child_questions_with_links:
        if not child.is_active():
            continue
        all_scored_pages.append(child)
        if child.id in cited_by_view_items:
            continue
        child_js = child_judgements_by_q.get(child.id, [])
        section = _classify_child_question(child, link, child_js)
        sections_dict[section].items.append(
            ViewItem(page=child, links=[link], section=section, source="graph")
        )

    for item_page, link in stored_items_with_links:
        if not item_page.is_active():
            continue
        section = link.section if link.section in sections_dict else "other"
        sections_dict[section].items.append(
            ViewItem(
                page=item_page,
                links=[link],
                section=section,
                source="view_item",
            )
        )

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

    return View(
        question=question,
        sections=sections,
        health=health,
        stored_view=stored_view,
    )


async def _fetch_direct(
    db: DB, question_id: str
) -> tuple[
    list[tuple[Page, PageLink]],
    list[tuple[Page, PageLink]],
    list[Page],
]:
    considerations = await db.get_considerations_for_question(question_id)
    child_questions_with_links = await db.get_child_questions_with_links(question_id)
    judgements = await db.get_judgements_for_question(question_id)
    return considerations, child_questions_with_links, judgements


async def _get_answers_links(db: DB, question_id: str) -> list[tuple[Page, PageLink]]:
    links = await db.get_links_to(question_id)
    answers_links = [lk for lk in links if lk.link_type == LinkType.ANSWERS]
    if not answers_links:
        return []
    pages = await db.get_pages_by_ids([lk.from_page_id for lk in answers_links])
    return [(pages[lk.from_page_id], lk) for lk in answers_links if lk.from_page_id in pages]


def _classify_consideration(claim: Page, link: PageLink, *, has_cites: bool) -> str:
    """Return the single section this consideration belongs in."""
    cred = claim.credence
    rob = claim.robustness

    if link.role == LinkRole.STRUCTURAL:
        return "broader_context"

    if has_cites or claim.provenance_call_type in _INGEST_CALL_TYPES:
        return "key_evidence"

    if cred is not None and cred >= 7 and rob is not None and rob >= 3:
        return "confident_views"

    if rob is not None and rob <= 2:
        return "live_hypotheses"

    if cred is not None and 4 <= cred <= 6:
        if rob is not None and rob >= 3:
            return "key_uncertainties"
        return "live_hypotheses"

    return "other"


def _classify_child_question(
    child: Page,
    link: PageLink,
    judgements: Sequence[Page],
) -> str:
    if link.role == LinkRole.STRUCTURAL:
        return "broader_context"

    active_judgements = [j for j in judgements if j.is_active()]
    if not active_judgements:
        return "key_uncertainties"

    return "assessments"


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
                if link.link_type == LinkType.CHILD_QUESTION and link.to_page_id not in visited:
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
    child_without_judgements = sum(
        1
        for child, _ in child_questions_with_links
        if child.is_active()
        and not any(j.is_active() for j in child_judgements_by_q.get(child.id, []))
    )
    return ViewHealth(
        total_pages=len(all_pages),
        missing_credence=missing_credence,
        child_questions_without_judgements=child_without_judgements,
        max_depth=max_depth,
    )
