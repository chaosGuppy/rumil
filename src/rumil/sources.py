"""
Source file reading, summarisation, and ingestion helpers.
"""

import logging
from pathlib import Path

from rumil.database import DB
from rumil.llm import text_call
from rumil.models import Page, PageLayer, PageType, Workspace
from rumil.orchestrators import ingest_until_done

log = logging.getLogger(__name__)


def read_file_content(path: Path) -> str:
    """Extract text from a file. Supports plain text and PDF."""
    if path.suffix.lower() == ".pdf":
        try:
            import pypdf  # type: ignore[reportMissingImports]
        except ImportError:
            raise RuntimeError(
                "pypdf is required for PDF ingestion: python -m pip install pypdf"
            )
        reader = pypdf.PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages)
    return path.read_text(encoding="utf-8")


async def generate_source_summary(content: str, filename: str) -> str:
    """Generate a 2-3 sentence LLM summary of a source document."""
    excerpt = content[:8000]
    if len(content) > 8000:
        excerpt += f"\n\n[Document truncated; full length: {len(content):,} chars]"
    try:
        result = await text_call(
            system_prompt=(
                "You summarize documents for a research workspace. "
                "Be concise and factual."
            ),
            user_message=(
                "Summarize this document in 2-3 sentences: what type of document is it, "
                "what is it about, and what would be its main relevance for research?\n\n"
                f"Filename: {filename}\n\n"
                f"{excerpt}"
            ),
        )
        return result.strip()
    except Exception as e:
        log.error("Source summary generation failed: %s", e, exc_info=True)
        print(f"  [ingest] Summary generation failed ({e}) — using filename.")
        return filename


async def create_source_page(filepath: str, db: DB) -> Page | None:
    """Read a file and create a Source page. Returns the page, or None on error."""
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
        epistemic_status=2.5,
        epistemic_type="ingested document",
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


async def run_ingest_calls(source_pages: list[Page], question_id: str, db: DB) -> int:
    """Run ingest extraction calls for each source against a question. Returns calls made."""
    made = 0
    for source_page in source_pages:
        if not await db.budget_remaining():
            print("  Budget exhausted — skipping remaining ingest extractions.")
            break
        rounds = await ingest_until_done(source_page, question_id, db)
        made += rounds
    return made
