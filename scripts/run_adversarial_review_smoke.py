"""Ad-hoc runner: fire a single AdversarialReviewCall against a target claim.

Smoke-test helper invoked by marketplace-thread/32. Kept minimal; not wired
into scripts/run_call.py because the regular CLI doesn't plumb through a
scope-page-id target without creating a fresh question first, which isn't
what adversarial_review wants.
"""

import argparse
import asyncio
import logging
import uuid

from rumil.calls.adversarial_review import AdversarialReviewCall
from rumil.database import DB
from rumil.models import CallType
from rumil.settings import get_settings


async def run(target_page_id: str, workspace: str, budget: int, staged: bool) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = get_settings()
    db = await DB.create(run_id=str(uuid.uuid4()), prod=False, staged=staged)
    project = await db.get_or_create_project(workspace)
    db.project_id = project.id

    target = await db.get_page(target_page_id)
    if target is None:
        raise SystemExit(f"target page {target_page_id!r} not found")
    if target.project_id and target.project_id != db.project_id:
        db.project_id = target.project_id

    print(f"Trace: {settings.frontend_url}/traces/{db.run_id}")
    print(f"Target: {target.id[:8]} [{target.page_type.value}] {target.headline}")

    await db.init_budget(budget)
    await db.create_run(
        name=f"adversarial-review-smoke:{target.id[:8]}",
        question_id=target.id,
        config=settings.capture_config(),
    )

    call = await db.create_call(
        CallType.ADVERSARIAL_REVIEW,
        scope_page_id=target.id,
    )
    runner = AdversarialReviewCall(target.id, call, db)
    await runner.run()

    print("\nDone.")
    print(f"Run ID: {db.run_id}")
    print(f"Call ID: {call.id}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("target_page_id", help="UUID of claim/judgement to review")
    parser.add_argument("--workspace", default="metr")
    parser.add_argument("--budget", type=int, default=8)
    parser.add_argument("--no-stage", dest="no_stage", action="store_true")
    args = parser.parse_args()
    asyncio.run(
        run(
            args.target_page_id,
            args.workspace,
            args.budget,
            staged=not args.no_stage,
        )
    )


if __name__ == "__main__":
    main()
