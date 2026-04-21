"""Typed graph deltas: what a process committed to the workspace.

A ``Delta`` is the structural summary of a process run's mutations:
which pages were created, which links were added, which pages were
superseded. Subclasses specialise the shape for a particular process
archetype (View-producing, variant-enumerating, map-producing).

Deltas are derived post-hoc by reading back rows tagged with the run's
``run_id``; see ``readback.py``. Some subclass fields (e.g. the
distinguished ``view_page_id`` or ``map_view_id``) are picked out by
convention during assembly — a page that's in ``new_pages`` may also
appear as the distinguished artifact.

Deltas allow partial population. An Investigator that ran out of budget
before synthesising a View reports ``view_page_id=None`` with its
scaffold still present; the process's ``Result.status`` carries the
incomplete signal.
"""

from typing import Literal

from pydantic import BaseModel

from rumil.models import LinkType, PageType


class PageRef(BaseModel):
    """Reference to a page touched by a process run."""

    page_id: str
    page_type: PageType
    headline: str = ""


class LinkRef(BaseModel):
    """Reference to a link created by a process run."""

    link_id: str
    from_page_id: str
    to_page_id: str
    link_type: LinkType


class SupersedeRef(BaseModel):
    old_page_id: str
    new_page_id: str


class _DeltaBase(BaseModel):
    new_pages: list[PageRef] = []
    new_links: list[LinkRef] = []
    supersedes: list[SupersedeRef] = []
    cited_page_ids: list[str] = []


class ViewDelta(_DeltaBase):
    """Output shape for Investigator-like processes.

    The distinguished artifact is ``view_page_id`` — a single View page
    synthesising the investigation. ``cited_page_ids`` records which
    existing pages the View is built on (links *from* the View). The
    scaffold (raw considerations gathered during exploration) lives in
    ``new_pages``; ``new_pages`` without a matching ``view_page_id``
    indicates an investigation that didn't finish its synthesis.
    """

    kind: Literal["view"] = "view"
    view_page_id: str | None = None


class VariantSetDelta(_DeltaBase):
    """Output shape for Robustifier-like processes.

    Enumerates variants of a source claim. No synthesis artifact; the
    variants themselves are the deliverable. ``source_claim_id`` is the
    claim being robustified; ``variant_ids`` is every variant page this
    run produced (all of which also appear in ``new_pages``).
    """

    kind: Literal["variant_set"] = "variant_set"
    source_claim_id: str
    variant_ids: list[str] = []


class MapDelta(_DeltaBase):
    """Output shape for Surveyor-like processes.

    Surveys a subgraph for cross-cutting structure. Optional
    ``map_view_id`` points at a synthesis View when one was produced;
    ``proposed_question_ids`` lists new cross-cutting questions the
    surveyor created; ``cross_link_ids`` lists newly-noticed inter-
    question edges. Most of the surveyor's real output will typically
    live in the accompanying ``Result.signals`` rather than in the
    delta.
    """

    kind: Literal["map"] = "map"
    map_view_id: str | None = None
    proposed_question_ids: list[str] = []
    cross_link_ids: list[str] = []


Delta = ViewDelta | VariantSetDelta | MapDelta
