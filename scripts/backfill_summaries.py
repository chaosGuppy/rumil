"""
Backfill abstracts for existing pages that don't have them.

Usage:
    uv run python scripts/backfill_summaries.py [--prod] [--dry-run] [--limit N] [--concurrency N]
"""

import argparse
import asyncio
import logging
import uuid

import anthropic
from anthropic.types import TextBlock
from dotenv import load_dotenv

from rumil.database import DB

load_dotenv()

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MODEL = "claude-sonnet-4-6"
CHUNK_SIZE = 100

SYSTEM = (
    "You are a precise information-distillation engine. Your output will be read by LLM instances "
    "that need to quickly understand the key findings of a research page. "
    "Prioritise accuracy, epistemic precision, and information density. "
    "Preserve: confidence levels, key qualifications, causal mechanisms, and priority orderings. "
    "Each summary must be fully self-contained — a reader with no prior context should understand "
    "what the page is about and what it concludes. "
    "Do not pad or soften. Output only the requested summaries, clearly labelled."
)

PROMPT_TEMPLATE = (
    "Produce an abstract (~200 words) of the research page below.\n\n"
    "Include the core conclusion, the main supporting reasoning or "
    "evidence, key counter-arguments and why they were discounted, and the critical "
    "uncertainties or dependencies. Preserve epistemic qualifications and confidence levels. "
    "Must be self-contained.\n\n"
    "Format your response exactly as:\n"
    "ABSTRACT: <text>\n\n"
    "Research page:\n"
    "{content}"
)


def _parse_response(text: str) -> str:
    if "ABSTRACT:" in text:
        return text[text.index("ABSTRACT:") + len("ABSTRACT:") :].strip()
    return ""


async def _fetch_all_needing_summaries(db: DB) -> list[dict]:
    """Paginate past the PostgREST 1000-row limit."""
    all_rows: list = []
    offset = 0
    while True:
        rows = (
            await db.client.table("pages")
            .select("id, page_type, headline, content")
            .eq("abstract", "")
            .eq("is_superseded", False)
            .order("created_at", desc=False)
            .range(offset, offset + CHUNK_SIZE - 1)
            .execute()
        ).data or []
        all_rows.extend(rows)
        if len(rows) < CHUNK_SIZE:
            break
        offset += CHUNK_SIZE
    return all_rows


async def _process_page(
    row: dict,
    idx: int,
    total: int,
    client: anthropic.AsyncAnthropic,
    db: DB,
    sem: asyncio.Semaphore,
    counters: dict,
) -> None:
    page_id = row["id"]
    page_text = f"# {row['headline']}\n\n{row['content']}"
    prompt = PROMPT_TEMPLATE.format(content=page_text)
    async with sem:
        try:
            response = await client.messages.create(
                model=MODEL,
                max_tokens=50_000,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            block = response.content[0]
            if not isinstance(block, TextBlock):
                log.warning("[%d/%d] Non-text response for %s", idx, total, page_id[:8])
                counters["failed"] += 1
                return
            abstract = _parse_response(block.text.strip())
            if not abstract:
                log.warning(
                    "[%d/%d] Parse failed for %s — raw: %s",
                    idx,
                    total,
                    page_id[:8],
                    block.text[:120],
                )
                counters["failed"] += 1
                return
            await db.update_page_abstract(page_id, abstract)
            log.info("[%d/%d] %s: %s", idx, total, page_id[:8], abstract[:80])
            counters["ok"] += 1
        except Exception as e:
            log.error("[%d/%d] Failed for %s: %s", idx, total, page_id[:8], e)
            counters["failed"] += 1


async def backfill(prod: bool, dry_run: bool, limit: int | None, concurrency: int) -> None:
    client = anthropic.AsyncAnthropic()
    db = await DB.create(run_id=str(uuid.uuid4()), prod=prod)

    rows = await _fetch_all_needing_summaries(db)
    if limit:
        rows = rows[:limit]

    log.info("Found %d pages needing summaries (concurrency=%d)", len(rows), concurrency)

    if dry_run:
        for r in rows:
            log.info("  [dry-run] [%s] %s", r["page_type"], r["headline"][:80])
        return

    sem = asyncio.Semaphore(concurrency)
    counters: dict[str, int] = {"ok": 0, "failed": 0}
    tasks = [
        _process_page(row, i + 1, len(rows), client, db, sem, counters)
        for i, row in enumerate(rows)
    ]
    await asyncio.gather(*tasks)

    log.info("Done. %d succeeded, %d failed.", counters["ok"], counters["failed"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prod", action="store_true", help="Target production database")
    parser.add_argument("--dry-run", action="store_true", help="List pages without writing")
    parser.add_argument("--limit", type=int, default=None, help="Max pages to process")
    parser.add_argument(
        "--concurrency", type=int, default=10, help="Parallel API calls (default 10)"
    )
    args = parser.parse_args()
    asyncio.run(backfill(args.prod, args.dry_run, args.limit, args.concurrency))
