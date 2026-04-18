"""SourceFirstOrchestrator: put sources before considerations, every iteration.

Design motivation
-----------------
A prior wave added a ``find_considerations_source_first`` prompt variant meant
to make find_considerations cite real external sources. That attempt didn't
work: the prioritizer never picked ``find_considerations`` for the source-first
arm, and even if it had, the find_considerations MOVES preset doesn't expose
``web_research`` or ``ingest`` as tools — those are separate call types
dispatched by the prioritizer.

The fix is to move "source-first" up a level: handle source sourcing at the
**orchestrator**, not the prompt. This orchestrator dispatches
``web_research`` (or ``ingest`` if the user pre-seeded URLs) to attach Source
pages to the question, then dispatches ``find_considerations`` with the
``source_first`` prompt variant temporarily activated. ``assess`` and
``update_view`` follow, then the loop iterates.

Loop, per iteration, until budget exhausts or the question stops producing
new sources:

  1. If zero sources attached to the question, look for a pre-seeded URL
     list on the question's ``extra`` field. If present, ingest each URL.
     Otherwise dispatch ``web_research``.
  2. Dispatch ``find_considerations`` with ``find_considerations_variant =
     "source_first"`` in effect. The prompt (maintained separately) tells
     the model to cite existing Source pages.
  3. Dispatch ``assess`` on the question.
  4. Refresh the view via ``create_view_for_question`` / ``update_view_for_question``.
  5. Loop back to step 1. Terminate if two consecutive iterations fail to
     produce new sources.

The key invariant: sources precede considerations in every iteration. If no
new sources are discoverable, the orchestrator lets find_considerations run
one more time against whatever sources already exist, then exits.
"""

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from rumil.database import DB
from rumil.models import Page
from rumil.orchestrators.base import BaseOrchestrator
from rumil.orchestrators.common import (
    assess_question,
    count_sources_for_question,
    create_view_for_question,
    find_considerations_until_done,
    ingest_until_done,
    update_view_for_question,
    web_research_question,
)
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster

log = logging.getLogger(__name__)


MAX_BARREN_SOURCE_ROUNDS = 2
MAX_ITERATIONS = 20
SOURCE_FIRST_VARIANT = "source_first"
SEED_URLS_EXTRA_KEY = "seed_source_urls"


@contextmanager
def _use_source_first_variant() -> Iterator[None]:
    """Temporarily set find_considerations_variant to source_first.

    The orchestrator flips the setting just for the find_considerations
    dispatch so the call's prompt loader picks up the source-first
    variant. Uses try/finally so nested exceptions don't leave the
    setting stuck.
    """
    settings = get_settings()
    previous = settings.find_considerations_variant
    settings.find_considerations_variant = SOURCE_FIRST_VARIANT
    try:
        yield
    finally:
        settings.find_considerations_variant = previous


class SourceFirstOrchestrator(BaseOrchestrator):
    """Dispatch sources, then considerations, then assess, then distill.

    Each iteration guarantees that any new Source pages are created before
    the find_considerations dispatch runs, so the source-first prompt
    variant has material to cite.
    """

    def __init__(
        self,
        db: DB,
        broadcaster: Broadcaster | None = None,
        budget_cap: int | None = None,
    ):
        super().__init__(db, broadcaster)
        self._budget_cap = budget_cap
        self._parent_call_id: str | None = None
        self._barren_source_rounds: int = 0

    async def _budget_remaining(self) -> int:
        remaining = await self.db.budget_remaining()
        if self._budget_cap is not None:
            return min(remaining, self._budget_cap)
        return remaining

    async def run(self, root_question_id: str) -> None:
        await self._setup()
        try:
            iteration = 0
            while iteration < MAX_ITERATIONS:
                if await self._budget_remaining() <= 0:
                    log.info("SourceFirst: budget exhausted, stopping")
                    break
                iteration += 1
                log.info(
                    "SourceFirst iteration %d: question=%s",
                    iteration,
                    root_question_id[:8],
                )
                progressed = await self._iterate(root_question_id)
                if not progressed:
                    log.info(
                        "SourceFirst: no progress for %d consecutive rounds, stopping",
                        self._barren_source_rounds,
                    )
                    break
        finally:
            await self._teardown()

    async def _iterate(self, question_id: str) -> bool:
        """Run one full source-first iteration.

        Returns False when the loop should terminate (e.g. repeated barren
        source rounds, budget exhausted mid-iteration before considerations
        could run).
        """
        sources_before = await count_sources_for_question(self.db, question_id)
        await self._dispatch_sources_if_needed(question_id, sources_before)
        sources_after = await count_sources_for_question(self.db, question_id)

        if sources_after <= sources_before and sources_before == 0:
            self._barren_source_rounds += 1
            log.info(
                "SourceFirst: no sources discovered (barren_rounds=%d)",
                self._barren_source_rounds,
            )
            if self._barren_source_rounds >= MAX_BARREN_SOURCE_ROUNDS:
                return False
        else:
            self._barren_source_rounds = 0

        if await self._budget_remaining() <= 0:
            return False

        with _use_source_first_variant():
            rounds, _ = await find_considerations_until_done(
                question_id,
                self.db,
                parent_call_id=self._parent_call_id,
                broadcaster=self.broadcaster,
            )
        log.info("SourceFirst: find_considerations ran for %d rounds", rounds)

        if await self._budget_remaining() <= 0:
            return True

        await assess_question(
            question_id,
            self.db,
            parent_call_id=self._parent_call_id,
            broadcaster=self.broadcaster,
            force=True,
        )

        await self._refresh_view(question_id)
        return True

    async def _dispatch_sources_if_needed(self, question_id: str, existing_sources: int) -> None:
        """Ingest pre-seeded URLs if present, else dispatch web_research."""
        if existing_sources > 0 and self._barren_source_rounds == 0:
            log.info(
                "SourceFirst: question already has %d sources, skipping sourcing pass",
                existing_sources,
            )
            return

        if await self._budget_remaining() <= 0:
            return

        seed_urls = await self._get_seed_urls(question_id)
        if seed_urls:
            log.info(
                "SourceFirst: ingesting %d pre-seeded URL(s) for question=%s",
                len(seed_urls),
                question_id[:8],
            )
            await self._ingest_seed_urls(question_id, seed_urls)
        else:
            log.info(
                "SourceFirst: dispatching web_research for question=%s",
                question_id[:8],
            )
            await web_research_question(
                question_id,
                self.db,
                parent_call_id=self._parent_call_id,
                broadcaster=self.broadcaster,
            )

    async def _get_seed_urls(self, question_id: str) -> Sequence[str]:
        """Return pre-seeded source URLs from the question's ``extra`` field.

        The orchestrator accepts ``extra["seed_source_urls"]`` as a list of
        URL strings. Anything else (missing key, wrong type, empty list)
        means "no pre-seeded URLs; fall back to web_research".
        """
        question = await self.db.get_page(question_id)
        if question is None:
            return []
        raw = (question.extra or {}).get(SEED_URLS_EXTRA_KEY)
        if not isinstance(raw, list):
            return []
        return [u for u in raw if isinstance(u, str) and u]

    async def _ingest_seed_urls(self, question_id: str, urls: Sequence[str]) -> None:
        """Create a Source page for each URL and run ingest against each.

        Imported lazily so tests that don't exercise this path don't pay
        the import cost (and can mock at the module boundary cleanly).
        """
        from rumil.sources import create_source_page

        for url in urls:
            if await self._budget_remaining() <= 0:
                return
            source_page = await create_source_page(url, self.db)
            if source_page is None:
                log.warning("SourceFirst: failed to create source page for %s", url)
                continue
            await ingest_until_done(
                source_page,
                question_id,
                self.db,
                parent_call_id=self._parent_call_id,
                broadcaster=self.broadcaster,
            )

    async def _refresh_view(self, question_id: str) -> Page | None:
        """Create or update the view for the question. Returns None; view is a side effect."""
        existing_view = await self.db.get_view_for_question(question_id)
        if existing_view is not None:
            await update_view_for_question(
                question_id,
                self.db,
                parent_call_id=self._parent_call_id,
                broadcaster=self.broadcaster,
                force=True,
            )
        else:
            await create_view_for_question(
                question_id,
                self.db,
                parent_call_id=self._parent_call_id,
                broadcaster=self.broadcaster,
                force=True,
            )
        return None


__all__ = ["SourceFirstOrchestrator"]
