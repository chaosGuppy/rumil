"""
Source file reading, summarisation, and ingestion helpers.
"""

import logging
from collections.abc import Sequence
from pathlib import Path

from rumil.database import DB
from rumil.llm import text_call
from rumil.models import Page, PageLayer, PageType, Workspace
from rumil.orchestrators import ingest_until_done
from rumil.scraper import scrape_url

log = logging.getLogger(__name__)

INGEST_MAX_CHARS = 500_000


def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def read_file_content(path: Path) -> str:
    """Extract text from a file. Supports plain text and PDF."""
    if path.suffix.lower() == ".pdf":
        try:
            import pypdf  # type: ignore[reportMissingImports]
        except ImportError as err:
            raise RuntimeError(
                "pypdf is required for PDF ingestion: python -m pip install pypdf"
            ) from err
        reader = pypdf.PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages)
    return path.read_text(encoding="utf-8")


async def generate_source_summary(content: str, source_label: str) -> str:
    """Generate a 2-3 sentence LLM summary of a source document."""
    excerpt = content[:8000]
    if len(content) > 8000:
        excerpt += f"\n\n[Document truncated; full length: {len(content):,} chars]"
    try:
        result = await text_call(
            system_prompt=(
                "You summarize documents for a research workspace. Be concise and factual."
            ),
            user_message=(
                "Summarize this document in 2-3 sentences: what type of document is it, "
                "what is it about, and what would be its main relevance for research?\n\n"
                f"Source: {source_label}\n\n"
                f"{excerpt}"
            ),
        )
        return result.strip()
    except Exception as e:
        log.error("Source summary generation failed: %s", e, exc_info=True)
        print(f"  [ingest] Summary generation failed ({e}) — using source label.")
        return source_label


async def create_source_page(source: str, db: DB) -> Page | None:
    """Read a file or URL and create a Source page. Returns the page, or None on error."""
    if _is_url(source):
        return await _create_source_page_from_url(source, db)
    return await _create_source_page_from_file(source, db)


async def _create_source_page_from_file(filepath: str, db: DB) -> Page | None:
    path = Path(filepath)
    if not path.exists():
        print(f"Error: file not found: {filepath}")
        return None
    try:
        content = read_file_content(path)
    except Exception as e:
        log.error("Failed to read file %s: %s", filepath, e, exc_info=True)
        print(f"Error reading {filepath}: {e}")
        return None

    print(f"  Summarising {path.name}...")
    summary = await generate_source_summary(content, path.name)
    page = Page(
        page_type=PageType.SOURCE,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=content,
        headline=summary,
        credence=5,
        robustness=1,
        provenance_model="human",
        provenance_call_type="ingest",
        provenance_call_id="manual",
        extra={"filename": path.name, "char_count": len(content)},
    )
    await db.save_page(page)
    print(f"\nSource created: {page.id}")
    print(f"File:           {path.name} ({len(content):,} chars)")
    print(f"Summary:        {summary[:120]}{'…' if len(summary) > 120 else ''}")
    return page


async def _create_source_page_from_url(url: str, db: DB) -> Page | None:
    print(f"  Fetching {url}...")
    scraped = await scrape_url(url, max_chars=INGEST_MAX_CHARS)
    if scraped is None:
        print(f"Error: failed to fetch URL: {url}")
        return None

    content = scraped.content
    title = scraped.title

    print(f"  Summarising {title}...")
    summary = await generate_source_summary(content, title)
    page = Page(
        page_type=PageType.SOURCE,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=content,
        headline=summary,
        credence=5,
        robustness=1,
        provenance_model="human",
        provenance_call_type="ingest",
        provenance_call_id="manual",
        extra={
            "filename": title,
            "url": scraped.url,
            "char_count": len(content),
            "fetched_at": scraped.fetched_at,
        },
    )
    await db.save_page(page)
    print(f"\nSource created: {page.id}")
    print(f"URL:            {scraped.url}")
    print(f"Title:          {title} ({len(content):,} chars)")
    print(f"Summary:        {summary[:120]}{'…' if len(summary) > 120 else ''}")
    return page


async def run_ingest_calls(source_pages: Sequence[Page], question_id: str, db: DB) -> int:
    """Run ingest extraction calls for each source against a question. Returns calls made."""
    made = 0
    for source_page in source_pages:
        if not await db.budget_remaining():
            print("  Budget exhausted — skipping remaining ingest extractions.")
            break
        rounds = await ingest_until_done(source_page, question_id, db)
        made += rounds
    return made
