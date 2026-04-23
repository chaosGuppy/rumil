"""Assemble typed deltas from a run_id, for wrapping existing orchs.

The wrap-first approach runs an existing orchestrator and then derives
a typed ``Delta`` by reading back rows tagged with the run's
``run_id``. This module centralises that derivation so each process
wrapper stays small.

The assembly is intentionally simple: ``new_pages`` / ``new_links`` /
``supersedes`` are copied straight from the DB, and the distinguishing
fields (``view_page_id``, ``variant_ids``, ``map_view_id``,
``proposed_question_ids``, ``cross_link_ids``) are picked out by page
type or link type. Citation extraction is best-effort: we treat links
*from* the distinguishing artifact (View / map View) as citations.
"""

from collections.abc import Sequence

from rumil.database import DB
from rumil.models import LinkType, Page, PageLink, PageType
from rumil.processes.delta import (
    LinkRef,
    MapDelta,
    PageRef,
    SupersedeRef,
    VariantSetDelta,
    ViewDelta,
)


async def _fetch_run_footprint(
    db: DB, run_id: str
) -> tuple[list[Page], list[PageLink], list[tuple[str, str]]]:
    """Fetch everything a run touched: new pages, new links, supersedes."""
    pages = await db.get_pages_for_run(run_id)
    links = await db.get_links_for_run(run_id)
    supersedes = await db.get_supersedes_for_run(run_id)
    return pages, links, supersedes


def _page_refs(pages: Sequence[Page]) -> list[PageRef]:
    return [PageRef(page_id=p.id, page_type=p.page_type, headline=p.headline) for p in pages]


def _link_refs(links: Sequence[PageLink]) -> list[LinkRef]:
    return [
        LinkRef(
            link_id=link.id,
            from_page_id=link.from_page_id,
            to_page_id=link.to_page_id,
            link_type=link.link_type,
        )
        for link in links
    ]


def _supersede_refs(pairs: Sequence[tuple[str, str]]) -> list[SupersedeRef]:
    return [SupersedeRef(old_page_id=old, new_page_id=new) for old, new in pairs]


def _pick_latest_view_for_question(
    pages: Sequence[Page], question_id: str, links: Sequence[PageLink]
) -> Page | None:
    """Pick the most recent View page this run created for *question_id*.

    A View is associated with its question via a VIEW_OF link (view ->
    question). Falls back to "any newest VIEW page in the run" when no
    VIEW_OF is present, which keeps the wrap robust against slight
    variations in how different orchs persist Views.
    """
    view_of_by_view = {
        link.from_page_id: link.to_page_id for link in links if link.link_type == LinkType.VIEW_OF
    }
    views = [p for p in pages if p.page_type == PageType.VIEW]
    if not views:
        return None
    matching = [v for v in views if view_of_by_view.get(v.id) == question_id]
    candidates = matching or views
    return max(candidates, key=lambda p: p.created_at)


def _citations_from(
    view_page_id: str | None, links: Sequence[PageLink], page_ids_in_run: set[str]
) -> list[str]:
    """Pages the View cites: links *from* the View to pages not created by this run.

    Pages created by this run are already in ``new_pages``; citations
    are the *existing* pages the View builds on.
    """
    if view_page_id is None:
        return []
    cited = [
        link.to_page_id
        for link in links
        if link.from_page_id == view_page_id and link.to_page_id not in page_ids_in_run
    ]
    seen: set[str] = set()
    out: list[str] = []
    for page_id in cited:
        if page_id not in seen:
            seen.add(page_id)
            out.append(page_id)
    return out


async def assemble_view_delta(db: DB, run_id: str, question_id: str) -> ViewDelta:
    pages, links, supersedes = await _fetch_run_footprint(db, run_id)
    view_page = _pick_latest_view_for_question(pages, question_id, links)
    view_page_id = view_page.id if view_page else None
    page_ids_in_run = {p.id for p in pages}
    return ViewDelta(
        new_pages=_page_refs(pages),
        new_links=_link_refs(links),
        supersedes=_supersede_refs(supersedes),
        cited_page_ids=_citations_from(view_page_id, links, page_ids_in_run),
        view_page_id=view_page_id,
    )


async def assemble_variant_set_delta(
    db: DB, run_id: str, source_claim_id: str, variant_ids: Sequence[str]
) -> VariantSetDelta:
    pages, links, supersedes = await _fetch_run_footprint(db, run_id)
    return VariantSetDelta(
        new_pages=_page_refs(pages),
        new_links=_link_refs(links),
        supersedes=_supersede_refs(supersedes),
        source_claim_id=source_claim_id,
        variant_ids=list(variant_ids),
    )


async def assemble_map_delta(db: DB, run_id: str) -> MapDelta:
    pages, links, supersedes = await _fetch_run_footprint(db, run_id)
    map_view = next(
        (
            p
            for p in sorted(pages, key=lambda p: p.created_at, reverse=True)
            if p.page_type == PageType.VIEW
        ),
        None,
    )
    map_view_id = map_view.id if map_view else None
    proposed_question_ids = [p.id for p in pages if p.page_type == PageType.QUESTION]
    cross_link_ids = [
        link.id
        for link in links
        if link.link_type in (LinkType.RELATED, LinkType.DEPENDS_ON, LinkType.CITES)
    ]
    page_ids_in_run = {p.id for p in pages}
    return MapDelta(
        new_pages=_page_refs(pages),
        new_links=_link_refs(links),
        supersedes=_supersede_refs(supersedes),
        cited_page_ids=_citations_from(map_view_id, links, page_ids_in_run),
        map_view_id=map_view_id,
        proposed_question_ids=proposed_question_ids,
        cross_link_ids=cross_link_ids,
    )
