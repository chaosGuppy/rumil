"""Per-unit budget consumption helper.

Lives in its own module to break the import cycle between
``rumil.orchestrators.common`` (which depends on the call modules) and
``rumil.calls.page_creators`` (which is one of those call modules but also
needs to debit budget per round).
"""

import logging

from rumil.database import DB

log = logging.getLogger(__name__)


async def _consume_budget(
    db: DB,
    force: bool = False,
    *,
    pool_question_id: str | None = None,
) -> bool:
    """Consume one unit of global budget. Returns False if exhausted.

    When *force* is True the call always succeeds: if normal consumption
    fails, budget is temporarily expanded so the dispatch can proceed.
    This is used to guarantee that every dispatch in a committed batch
    runs, even if it means slightly exceeding the original budget.

    When *pool_question_id* is set, also debit the per-question budget pool
    for that question. The pool debit never refuses; the run-level budget
    is the authoritative gate.
    """
    ok = await db.consume_budget(1)
    if not ok:
        if force:
            await db.add_budget(1)
            ok = await db.consume_budget(1)
        if not ok:
            remaining = await db.budget_remaining()
            log.info("Budget exhausted (remaining: %d)", remaining)
    if ok and pool_question_id is not None:
        await db.qbp_consume(pool_question_id, 1)
    return ok
