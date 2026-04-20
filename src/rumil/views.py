"""
View construction for research questions.

Builds a structured View of research on a question as a function of
workspace state: graph-derived items (considerations, child questions,
judgements) merged with stored VIEW_ITEM pages from update_view when
available. Always works — no update_view pass required — but honors
curation where it exists.
"""

import asyncio
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
    links: Sequence[PageLink]
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
    sections: Sequence[ViewSection]
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

    Thin wrapper around :func:`build_views_many` — prefer that for batched
    use to avoid per-qid query fan-out.
    """
    result = await build_views_many(db, [question_id])
    return result[question_id]


async def build_views_many(
    db: DB,
    question_ids: Sequence[str],
) -> dict[str, View]:
    """Build Views for many questions with batched DB fetches.

    Round trips are O(1) in the number of questions for the bulk fetches
    (questions, considerations, child questions, judgements, views, and
    cross-page outgoing links). View items per stored view and depth BFS
    per root remain O(N) calls, fired in parallel via ``asyncio.gather``.
    """
    if not question_ids:
        return {}

    qids = list(dict.fromkeys(question_ids))

    questions_map = await db.get_pages_by_ids(qids)
    for qid in qids:
        q = questions_map.get(qid)
        if not q:
            raise ValueError(f"Question {qid} not found")
        if q.page_type != PageType.QUESTION:
            raise ValueError(f"Page {qid[:8]} is {q.page_type.value}, not a question")

    (
        considerations_by_q,
        child_qs_with_links_by_q,
        judgements_by_q,
        stored_view_by_q,
    ) = await asyncio.gather(
        db.get_considerations_for_questions(qids),
        _get_child_questions_with_links_many(db, qids),
        db.get_judgements_for_questions(qids),
        db.get_views_for_questions(qids),
    )

    all_child_ids: list[str] = []
    for q_list in child_qs_with_links_by_q.values():
        all_child_ids.extend(c.id for c, _ in q_list)
    child_judgements_by_q: dict[str, list[Page]] = {}
    if all_child_ids:
        child_judgements_by_q = await db.get_judgements_for_questions(all_child_ids)

    all_consideration_ids: list[str] = []
    for q_list in considerations_by_q.values():
        all_consideration_ids.extend(p.id for p, _ in q_list)
    outgoing_by_consideration = (
        await db.get_links_from_many(all_consideration_ids) if all_consideration_ids else {}
    )
    cites_page_ids = {
        pid
        for pid, links in outgoing_by_consideration.items()
        if any(l.link_type == LinkType.CITES for l in links)
    }

    views_with_items = [v for v in stored_view_by_q.values() if v is not None]
    stored_items_by_view: dict[str, Sequence[tuple[Page, PageLink]]] = {}
    cited_by_view_items_all: set[str] = set()
    if views_with_items:
        items_results = await asyncio.gather(*(db.get_view_items(v.id) for v in views_with_items))
        stored_items_by_view = dict(zip([v.id for v in views_with_items], items_results))
        all_view_item_ids = [p.id for items in stored_items_by_view.values() for p, _ in items]
        view_item_outgoing = (
            await db.get_links_from_many(all_view_item_ids) if all_view_item_ids else {}
        )
        for links in view_item_outgoing.values():
            for link in links:
                if link.link_type in (LinkType.CITES, LinkType.DEPENDS_ON):
                    cited_by_view_items_all.add(link.to_page_id)

    qs_with_active_judgements = [
        qid for qid in qids if any(j.is_active() for j in judgements_by_q.get(qid, []))
    ]
    answers_links_by_q: dict[str, Sequence[tuple[Page, PageLink]]] = {qid: [] for qid in qids}
    if qs_with_active_judgements:
        answers_results = await asyncio.gather(
            *(_get_answers_links(db, qid) for qid in qs_with_active_judgements)
        )
        answers_links_by_q.update(dict(zip(qs_with_active_judgements, answers_results)))

    depths = await asyncio.gather(*(_measure_child_depth(db, qid) for qid in qids))
    depth_by_q = dict(zip(qids, depths))

    result: dict[str, View] = {}
    for qid in qids:
        question = questions_map[qid]
        result[qid] = _assemble_view(
            question=question,
            considerations=considerations_by_q.get(qid, []),
            child_questions_with_links=child_qs_with_links_by_q.get(qid, []),
            judgements=judgements_by_q.get(qid, []),
            child_judgements_by_q=child_judgements_by_q,
            cites_page_ids=cites_page_ids,
            stored_view=stored_view_by_q.get(qid),
            stored_items_with_links=(
                stored_items_by_view.get(stored_view_by_q.get(qid).id, [])  # type: ignore[union-attr]
                if stored_view_by_q.get(qid)
                else []
            ),
            cited_by_view_items=cited_by_view_items_all,
            answers_links=answers_links_by_q.get(qid, []),
            max_depth=depth_by_q[qid],
        )
    return result


def _assemble_view(
    *,
    question: Page,
    considerations: Sequence[tuple[Page, PageLink]],
    child_questions_with_links: Sequence[tuple[Page, PageLink]],
    judgements: Sequence[Page],
    child_judgements_by_q: dict[str, list[Page]],
    cites_page_ids: set[str],
    stored_view: Page | None,
    stored_items_with_links: Sequence[tuple[Page, PageLink]],
    cited_by_view_items: set[str],
    answers_links: Sequence[tuple[Page, PageLink]],
    max_depth: int,
) -> View:
    sections_dict: dict[str, ViewSection] = {
        name: ViewSection(name=name) for name in DEFAULT_VIEW_SECTIONS
    }
    all_scored_pages: list[Page] = []

    active_judgements = [j for j in judgements if j.is_active()]
    if active_judgements:
        latest = max(active_judgements, key=lambda j: j.created_at)
        judgement_links = [link for p, link in answers_links if p.id == latest.id]
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
        section = _classify_consideration(claim, link, has_cites=claim.id in cites_page_ids)
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


async def _get_child_questions_with_links_many(
    db: DB,
    question_ids: Sequence[str],
) -> dict[str, Sequence[tuple[Page, PageLink]]]:
    """Batched equivalent of get_child_questions_with_links across many parents."""
    result: dict[str, Sequence[tuple[Page, PageLink]]] = {qid: [] for qid in question_ids}
    if not question_ids:
        return result
    links_by_parent = await db.get_links_from_many(list(question_ids))
    child_ids: list[str] = []
    child_links_by_parent: dict[str, list[PageLink]] = {}
    for qid in question_ids:
        kids = [l for l in links_by_parent.get(qid, []) if l.link_type == LinkType.CHILD_QUESTION]
        if kids:
            child_links_by_parent[qid] = kids
            child_ids.extend(l.to_page_id for l in kids)
    if not child_ids:
        return result
    pages = await db.get_pages_by_ids(list(dict.fromkeys(child_ids)))
    for qid, kids in child_links_by_parent.items():
        result[qid] = [(pages[l.to_page_id], l) for l in kids if l.to_page_id in pages]
    return result


async def _get_answers_links(db: DB, question_id: str) -> Sequence[tuple[Page, PageLink]]:
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
