"""
RobustifyOrchestrator: iteratively produce high-credence, substantive claim variants.

Loop:
  1. Run scout_c_robustify → variant claims
  2. Run how-true + how-false scouts on each variant, then assess
  3. If any variant reaches credence ≥ 8, do one more round (strengthen),
     then stop. If not, robustify again.
  4. Stop after max_rounds.
"""

import asyncio
import logging
from collections.abc import Sequence

from rumil.calls.scout_c_how_false import ScoutCHowFalseCall
from rumil.calls.scout_c_how_true import ScoutCHowTrueCall
from rumil.calls.scout_c_robustify import ScoutCRobustifyCall
from rumil.calls.scout_c_strengthen import ScoutCStrengthenCall
from rumil.calls.stages import CallRunner
from rumil.database import DB
from rumil.models import CallType
from rumil.orchestrators.common import assess_question
from rumil.tracing.broadcast import Broadcaster

log = logging.getLogger(__name__)

CREDENCE_THRESHOLD = 8
SCOUT_MAX_ROUNDS = 2
SCOUT_FRUIT_THRESHOLD = 4
DEFAULT_MAX_ROUNDS = 5


class RobustifyOrchestrator:
    """Iteratively produce high-credence, substantive variants of a claim."""

    def __init__(
        self,
        db: DB,
        broadcaster: Broadcaster | None = None,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
    ):
        self.db = db
        self.broadcaster = broadcaster
        self.max_rounds = max_rounds

    async def run(self, claim_id: str) -> Sequence[str]:
        """Run the robustify loop. Returns IDs of variant claims produced."""
        own_db = await self.db.fork()
        try:
            return await self._run_loop(claim_id, own_db)
        finally:
            await own_db.close()

    async def _run_loop(self, claim_id: str, db: DB) -> Sequence[str]:
        all_variant_ids: list[str] = []
        round_num = 0
        hit_threshold = False

        for round_num in range(1, self.max_rounds + 1):
            log.info(
                "RobustifyOrchestrator round %d/%d: claim=%s",
                round_num,
                self.max_rounds,
                claim_id[:8],
            )

            if round_num == 1:
                ids_to_test = await self._run_scout(
                    claim_id,
                    db,
                    CallType.SCOUT_C_ROBUSTIFY,
                    ScoutCRobustifyCall,
                )
                if not ids_to_test:
                    log.info("RobustifyOrchestrator: no variants produced, stopping")
                    break
                all_variant_ids.extend(ids_to_test)
            else:
                next_round_ids: list[str] = []
                for vid in all_variant_ids:
                    credence = await self._get_credence(vid, db)
                    if credence >= CREDENCE_THRESHOLD:
                        new_ids = await self._run_scout(
                            vid,
                            db,
                            CallType.SCOUT_C_STRENGTHEN,
                            ScoutCStrengthenCall,
                        )
                        next_round_ids.extend(new_ids)
                    else:
                        new_ids = await self._run_scout(
                            vid,
                            db,
                            CallType.SCOUT_C_ROBUSTIFY,
                            ScoutCRobustifyCall,
                        )
                        next_round_ids.extend(new_ids)

                if next_round_ids:
                    all_variant_ids.extend(next_round_ids)

                ids_to_test = next_round_ids if next_round_ids else all_variant_ids
                if not ids_to_test:
                    break

            await self._investigate_variants(ids_to_test, db)

            if hit_threshold:
                log.info(
                    "RobustifyOrchestrator: completed extra round after "
                    "reaching credence threshold, stopping",
                )
                break

            for vid in all_variant_ids:
                if await self._get_credence(vid, db) >= CREDENCE_THRESHOLD:
                    hit_threshold = True
                    break

            if hit_threshold:
                log.info(
                    "RobustifyOrchestrator: credence >= %d reached, will do one more round",
                    CREDENCE_THRESHOLD,
                )

        log.info(
            "RobustifyOrchestrator complete: %d variants produced over %d rounds",
            len(all_variant_ids),
            round_num,
        )
        return all_variant_ids

    async def _run_scout(
        self,
        claim_id: str,
        db: DB,
        call_type: CallType,
        cls: type[CallRunner],
    ) -> list[str]:
        """Run a scout call and return created page IDs."""
        call = await db.create_call(call_type, scope_page_id=claim_id)
        runner = cls(
            claim_id,
            call,
            db,
            broadcaster=self.broadcaster,
        )
        await runner.run()
        created = list(runner.result.created_page_ids)
        log.info(
            "%s on %s produced %d pages",
            call_type.value,
            claim_id[:8],
            len(created),
        )
        return created

    async def _investigate_variants(
        self,
        variant_ids: Sequence[str],
        db: DB,
    ) -> None:
        """Run how-true + how-false scouts then assess on each variant, concurrently."""
        tasks = [self._investigate_one(vid, db) for vid in variant_ids]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, r in enumerate(results):
                if isinstance(r, Exception):
                    log.error(
                        "Investigation failed for variant %s: %s",
                        variant_ids[i][:8],
                        r,
                        exc_info=r,
                    )

    async def _investigate_one(self, variant_id: str, db: DB) -> None:
        """Run how-true and how-false scouts concurrently, then assess."""
        how_true_call = await db.create_call(
            CallType.SCOUT_C_HOW_TRUE,
            scope_page_id=variant_id,
        )
        how_false_call = await db.create_call(
            CallType.SCOUT_C_HOW_FALSE,
            scope_page_id=variant_id,
        )

        how_true_runner = ScoutCHowTrueCall(
            variant_id,
            how_true_call,
            db,
            broadcaster=self.broadcaster,
            max_rounds=SCOUT_MAX_ROUNDS,
            fruit_threshold=SCOUT_FRUIT_THRESHOLD,
        )
        how_false_runner = ScoutCHowFalseCall(
            variant_id,
            how_false_call,
            db,
            broadcaster=self.broadcaster,
            max_rounds=SCOUT_MAX_ROUNDS,
            fruit_threshold=SCOUT_FRUIT_THRESHOLD,
        )

        await asyncio.gather(how_true_runner.run(), how_false_runner.run())

        await assess_question(
            variant_id,
            db,
            broadcaster=self.broadcaster,
            force=True,
        )

    async def _get_credence(self, page_id: str, db: DB) -> int:
        """Read current credence for a page. Returns 5 (neutral) if not found."""
        page = await db.get_page(page_id)
        if page and page.credence is not None:
            return page.credence
        return 5
