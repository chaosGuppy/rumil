"""Abstract `View`: ever-evolving best summary of a question.

Concrete implementations decide *how* the summary is stored and rendered
(sectioned items with importance caps, flat NL judgements, etc.). This ABC
fixes the lifecycle (when to refresh) and the rendering surfaces (where the
summary appears in prompts).
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime

from rumil.database import DB
from rumil.models import Page
from rumil.tracing.broadcast import Broadcaster


class View(ABC):
    """Ever-evolving best summary of a question. Pluggable implementation."""

    @abstractmethod
    async def exists(self, question_id: str, db: DB) -> bool:
        """True if this variant already has a refreshable summary for *question_id*."""

    @abstractmethod
    async def headline_page(self, question_id: str, db: DB) -> Page | None:
        """The single canonical page representing the overall take on *question_id*.

        For ``JudgementView`` this is the latest active judgement; for
        ``SectionedView`` this is the active VIEW page. Callers needing the
        page id (e.g. to exclude from a subgraph render) or its robustness
        should use this rather than reaching for variant-specific accessors.
        Returns None when no take has been recorded yet.
        """

    @abstractmethod
    async def headline_pages_many(
        self,
        question_ids: Sequence[str],
        db: DB,
    ) -> dict[str, Page | None]:
        """Batch counterpart to :meth:`headline_page` for traversal hot loops.

        Returns ``{question_id: headline_page_or_None}`` for every input id,
        issuing a bounded number of queries regardless of input size.
        """

    @abstractmethod
    async def render_for_executive_summary(
        self,
        question_id: str,
        db: DB,
    ) -> str | None:
        """Full-detail render of the overall take, for executive-summary contexts.

        Used by ``calls/summarize.py`` for the headline question. Returns
        None when no take has been recorded yet.
        """

    @abstractmethod
    async def refresh(
        self,
        question_id: str,
        db: DB,
        *,
        parent_call_id: str | None = None,
        context_page_ids: Sequence[str] | None = None,
        broadcaster: Broadcaster | None = None,
        force: bool = False,
        call_id: str | None = None,
        sequence_id: str | None = None,
        sequence_position: int | None = None,
        pool_question_id: str | None = None,
    ) -> str | None:
        """Create-if-missing or update. Single entry point for all refresh sites.

        Returns the new call ID, or None if skipped (e.g. budget exhausted).
        """

    @abstractmethod
    async def render_for_prioritization(self, question_id: str, db: DB) -> str | None:
        """Compact render for the prioritization-context scope header.

        Returns None if this variant has no data for the question.
        """

    @abstractmethod
    async def render_for_parent_scoring(self, question_id: str, db: DB) -> str | None:
        """Render when *question_id* is the parent in score_items_sequentially.

        Returns None if this variant has no data for the parent.
        """

    @abstractmethod
    async def render_for_child_investigation_results(
        self,
        question_id: str,
        db: DB,
        *,
        last_view_created_at: datetime | None,
    ) -> tuple[bool, str, list[str]] | None:
        """Render *question_id*'s current state as a child investigation result.

        Returns ``(is_new, rendered_block, cited_page_ids)`` or None if this
        variant has no data for the child. ``is_new`` is True when the
        underlying data post-dates *last_view_created_at* (or when it is None).
        """
