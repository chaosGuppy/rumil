"""Backfill summary embeddings for all pages in the database.

Usage:
    uv run python scripts/backfill_summary_embeddings.py
    uv run python scripts/backfill_summary_embeddings.py --prod
    uv run python scripts/backfill_summary_embeddings.py --batch-size 100
"""

import argparse
import asyncio
import logging
import uuid

from rumil.database import DB
from rumil.embeddings import backfill_embeddings


async def run(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    db = await DB.create(run_id=str(uuid.uuid4()), prod=args.prod)

    total = 0
    while True:
        count = await backfill_embeddings(
            db, field_name="abstract", batch_size=args.batch_size,
        )
        total += count
        if count < args.batch_size:
            break
        print(f"  ...embedded {total} pages so far")

    print(f"Done. Embedded summaries for {total} pages.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill summary embeddings for all pages.",
    )
    parser.add_argument(
        "--prod", action="store_true",
        help="Run against production database",
    )
    parser.add_argument(
        "--batch-size", type=int, default=50,
        help="Pages per batch (default: 50)",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
