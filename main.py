"""
Research Workspace — entry point.

Modes:
    python main.py "Your question here" --budget 20     # new investigation
    python main.py --continue QUESTION_ID --budget 10   # add budget to existing question
    python main.py --list                               # show existing questions

Set ANTHROPIC_API_KEY in your environment before running.
"""

import argparse
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from differential.database import DB
from differential.models import Page, PageLayer, PageLink, PageType, LinkType, Workspace
from differential.orchestrator import Orchestrator, ingest_until_done
from differential.chat import run_chat
from differential.mapper import generate_map
from differential.summary import generate_summary, save_summary
from differential import tracer
from differential.tracer import generate_trace

log = logging.getLogger(__name__)

PAGES_DIR = Path(__file__).parent / "pages"


async def create_root_question(question_text: str, db: DB) -> str:
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=question_text,
        summary=question_text[:120],
        epistemic_status=2.5,
        epistemic_type="open question",
        provenance_model="human",
        provenance_call_type="init",
        provenance_call_id="init",
        extra={"status": "open"},
    )
    await db.save_page(page)
    return page.id


async def cmd_add_question(
    question_text: str, parent_id: str | None, budget: int | None, db: DB
) -> None:
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=question_text,
        summary=question_text[:120],
        epistemic_status=2.5,
        epistemic_type="open question",
        provenance_model="human",
        provenance_call_type="manual",
        provenance_call_id="manual",
        extra={"status": "open"},
    )
    await db.save_page(page)

    if parent_id:
        parent = await db.get_page(parent_id)
        if not parent:
            print(
                f"Warning: parent '{parent_id}' not found — question created without parent link."
            )
        else:
            link = PageLink(
                from_page_id=parent_id,
                to_page_id=page.id,
                link_type=LinkType.CHILD_QUESTION,
                reasoning="Manually added sub-question",
            )
            await db.save_link(link)
            print(f"\nAdded as sub-question of: {parent.summary[:70]}")

    print(f"\nQuestion added: {page.id}")
    print(f"Text:           {question_text}")

    effective_budget = 5 if budget is None else budget
    if effective_budget > 0:
        print(
            f"Budget:         {effective_budget} research call{'s' if effective_budget != 1 else ''}\n"
        )
        await db.init_budget(effective_budget)
        await Orchestrator(db).run(page.id)
        await _print_summary(db)
    else:
        print("\nTo investigate it later:")
        print(f"  python main.py --continue {page.id} --budget N")


def _read_file_content(path: Path) -> str:
    """Extract text from a file. Supports plain text and PDF."""
    if path.suffix.lower() == ".pdf":
        try:
            import pypdf
        except ImportError:
            raise RuntimeError(
                "pypdf is required for PDF ingestion: python -m pip install pypdf"
            )
        reader = pypdf.PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages)
    return path.read_text(encoding="utf-8")


def _generate_source_summary(content: str, filename: str) -> str:
    """Generate a 2-3 sentence LLM summary of a source document."""
    from llm import run_llm

    excerpt = content[:8000]
    if len(content) > 8000:
        excerpt += f"\n\n[Document truncated; full length: {len(content):,} chars]"
    try:
        return run_llm(
            system_prompt="You summarize documents for a research workspace. Be concise and factual.",
            user_message=(
                f"Summarize this document in 2-3 sentences: what type of document is it, "
                f"what is it about, and what would be its main relevance for research?\n\n"
                f"Filename: {filename}\n\n{excerpt}"
            ),
            max_tokens=256,
        ).strip()
    except Exception as e:
        log.error("Source summary generation failed: %s", e, exc_info=True)
        print(f"  [ingest] Summary generation failed ({e}) — using filename.")
        return filename


async def _create_source_page(filepath: str, db: DB) -> Page | None:
    """Read a file and create a Source page. Returns the page, or None on error."""
    path = Path(filepath)
    if not path.exists():
        print(f"Error: file not found: {filepath}")
        return None
    try:
        content = _read_file_content(path)
    except Exception as e:
        log.error("Failed to read file %s: %s", filepath, e, exc_info=True)
        print(f"Error reading {filepath}: {e}")
        return None

    print(f"  Summarising {path.name}...")
    summary = _generate_source_summary(content, path.name)
    page = Page(
        page_type=PageType.SOURCE,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=content,
        summary=summary,
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


async def _run_ingest_calls(source_pages: list[Page], question_id: str, db: DB) -> int:
    """Run ingest extraction calls for each source against a question. Returns calls made."""
    made = 0
    for source_page in source_pages:
        if not await db.budget_remaining():
            print("  Budget exhausted — skipping remaining ingest extractions.")
            break
        rounds = await ingest_until_done(source_page, question_id, db)
        made += rounds
    return made


async def cmd_ingest(
    ingest_files: list[str], for_question_id: str | None, budget: int | None, db: DB
) -> None:
    # Create source pages first (always free)
    source_pages = []
    for filepath in ingest_files:
        page = await _create_source_page(filepath, db)
        if page:
            source_pages.append(page)

    if not source_pages:
        return

    if not for_question_id:
        print(
            "\nSources stored. Use --for-question QUESTION_ID to extract considerations."
        )
        print(
            "To investigate later:  python main.py --ingest FILE --for-question ID --budget N"
        )
        return

    question = await db.get_page(for_question_id)
    if not question:
        print(
            f"Error: question '{for_question_id}' not found. Run --list to see existing questions."
        )
        return

    effective_budget = len(source_pages) if budget is None else budget
    if effective_budget == 0:
        print("\nSources stored (--budget 0, no extraction).")
        return

    print(f"\nExtracting considerations for: {question.summary[:80]}")
    print(f"Budget: {effective_budget} call{'s' if effective_budget != 1 else ''}\n")
    await db.init_budget(effective_budget)
    made = await _run_ingest_calls(source_pages, for_question_id, db)
    total, used = await db.get_budget()
    print(f"\nIngest complete. {made} extraction call{'s' if made != 1 else ''} made.")
    print(f"Budget used: {used}/{total}")
    print("\nRun --map or --chat to explore the results.")


async def cmd_map(question_id: str, db: DB) -> None:
    question = await db.get_page(question_id)
    if not question:
        print(
            f"Error: question '{question_id}' not found. Run --list to see existing questions."
        )
        sys.exit(1)
    print(f"\nGenerating map for: {question.summary[:80]}")
    path = await generate_map(question_id, db)
    print(f"Map saved to: {path}")
    print("Open that file in your browser to view it.")


async def cmd_trace(trace_id: str, db: DB) -> None:
    path = await generate_trace(trace_id, db)
    print(f"Trace saved to: {path}")
    print("Open that file in your browser to view it.")


async def cmd_summary(question_id: str, db: DB) -> None:
    question = await db.get_page(question_id)
    if not question:
        print(
            f"Error: question '{question_id}' not found. Run --list to see existing questions."
        )
        sys.exit(1)

    print(f"\nGenerating summary for: {question.summary[:80]}")
    print("(This will use one LLM call but does not count against research budget)\n")

    summary_text = await generate_summary(question_id, db)
    path = save_summary(summary_text, question.summary)

    print(summary_text)
    print(f"\n---\nSummary saved to: {path}")


async def cmd_list(db: DB, workspace_name: str) -> None:
    questions = await db.get_root_questions()
    if not questions:
        print(f"No questions in workspace '{workspace_name}'.")
        return

    print(f"\nWorkspace: {workspace_name}")
    print(f"\n{'ID':38}  {'Cons':>4}  {'Judg':>4}  Question")
    print("-" * 100)
    for q in questions:
        counts = await db.count_pages_for_question(q.id)
        truncated = q.summary[:55] + "…" if len(q.summary) > 55 else q.summary
        print(
            f"{q.id}  {counts['considerations']:>4}  {counts['judgements']:>4}  {truncated}"
        )
    print("\nTo continue investigating a question:")
    print("  python main.py --continue QUESTION_ID --budget N")


async def cmd_list_workspaces(db: DB) -> None:
    projects = await db.list_projects()
    if not projects:
        print("No workspaces yet.")
        return
    print(f"\n{'Name':20}  {'Created':20}  ID")
    print("-" * 80)
    for p in projects:
        print(f"{p.name:20}  {p.created_at.strftime('%Y-%m-%d %H:%M'):20}  {p.id}")


async def cmd_new(
    question_text: str,
    budget: int | None,
    db: DB,
    ingest_files: list[str] | None = None,
) -> None:
    budget = budget if budget is not None else 10
    await db.init_budget(budget)
    question_id = await create_root_question(question_text, db)

    print(f"\nNew question: {question_id}")
    print(f"Question:     {question_text}")
    print(f"Budget:       {budget} research calls")

    if ingest_files:
        source_pages = []
        for filepath in ingest_files:
            page = await _create_source_page(filepath, db)
            if page:
                source_pages.append(page)
        if source_pages:
            print(f"\nIngesting {len(source_pages)} source file(s)...")
            await _run_ingest_calls(source_pages, question_id, db)

    await Orchestrator(db).run(question_id)
    await _print_summary(db)


def _batch_label(entry: dict) -> str:
    if "continue" in entry:
        return f"continue {entry['continue'][:8]}..."
    return entry["question"][:70]


async def _run_one_batch_entry(
    entry: dict, index: int, total: int, template_db: DB
) -> None:
    """Run a single batch entry with its own run_id for budget isolation."""
    budget = entry.get("budget", 10)
    label = _batch_label(entry)

    db = await DB.create(
        run_id=str(uuid.uuid4()),
        client=template_db.client,
        project_id=template_db.project_id,
    )

    print(f"\n[{index + 1}/{total}] Starting: {label} (budget={budget})")

    if "continue" in entry:
        await cmd_continue(entry["continue"], budget, db)
    else:
        await cmd_new(entry["question"], budget, db, ingest_files=entry.get("ingest"))

    print(f"\n[{index + 1}/{total}] Done: {label}")


async def cmd_batch(batch_file: str, db: DB) -> None:
    path = Path(batch_file)
    if not path.exists():
        print(f"Error: file not found: {batch_file}")
        sys.exit(1)

    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error reading batch file: {e}")
        sys.exit(1)

    if not isinstance(entries, list) or not entries:
        print("Error: batch file must contain a non-empty JSON array.")
        sys.exit(1)

    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            print(f"Error: entry {i} must be a JSON object.")
            sys.exit(1)
        if "question" not in entry and "continue" not in entry:
            print(f"Error: entry {i} must have a 'question' or 'continue' field.")
            sys.exit(1)

    total_budget = sum(e.get("budget", 10) for e in entries)
    new_count = sum(1 for e in entries if "question" in e)
    cont_count = sum(1 for e in entries if "continue" in e)
    parts = []
    if new_count:
        parts.append(f"{new_count} new")
    if cont_count:
        parts.append(f"{cont_count} continue")
    print(f"\nBatch: {' + '.join(parts)}, total budget {total_budget}")
    print("Running concurrently...\n")

    tasks = [
        _run_one_batch_entry(entry, i, len(entries), db)
        for i, entry in enumerate(entries)
    ]
    await asyncio.gather(*tasks)


async def cmd_continue(question_id: str, additional_budget: int | None, db: DB) -> None:
    additional_budget = additional_budget if additional_budget is not None else 10
    question = await db.get_page(question_id)
    if not question:
        print(
            f"Error: question '{question_id}' not found. Run --list to see existing questions."
        )
        sys.exit(1)
    if question.page_type != PageType.QUESTION:
        print(
            f"Error: page '{question_id}' is a {question.page_type.value}, not a question."
        )
        sys.exit(1)

    counts = await db.count_pages_for_question(question_id)
    await db.init_budget(additional_budget)

    print(f"\nContinuing investigation of: {question.summary[:80]}")
    print(f"Question ID:  {question_id}")
    print(
        f"Existing:     {counts['considerations']} considerations, {counts['judgements']} judgements"
    )
    print(f"Budget:       {additional_budget} research calls")

    await Orchestrator(db).run(question_id)
    await _print_summary(db)


async def _print_summary(db: DB) -> None:
    total, used = await db.get_budget()
    print(f"\nPages written to: {PAGES_DIR}")
    print(f"Budget used:      {used}/{total} calls")
    print("\nRun --list to see all questions.")


async def async_main():
    parser = argparse.ArgumentParser(
        description="Research workspace.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python main.py "Should I use solar panels?" --budget 20\n'
            "  python main.py --list\n"
            "  python main.py --continue abc12345-... --budget 10"
        ),
    )
    parser.add_argument("question", nargs="?", help="Question to investigate (new run)")
    parser.add_argument(
        "--budget",
        type=int,
        default=None,
        help="Research call budget. With --add-question: defaults to 5, "
        "pass 0 to add without investigating. "
        "With --continue/new question: defaults to 10.",
    )
    parser.add_argument(
        "--continue",
        dest="continue_id",
        metavar="QUESTION_ID",
        help="Continue investigating an existing question",
    )
    parser.add_argument(
        "--list", action="store_true", help="List existing questions in the workspace"
    )
    parser.add_argument(
        "--summary",
        dest="summary_id",
        metavar="QUESTION_ID",
        help="Generate an executive summary for a question",
    )
    parser.add_argument(
        "--map",
        dest="map_id",
        metavar="QUESTION_ID",
        help="Generate a visual HTML map of the research tree",
    )
    parser.add_argument(
        "--chat",
        dest="chat_id",
        metavar="QUESTION_ID",
        help="Chat interactively about the research on a question",
    )
    parser.add_argument(
        "--trace",
        dest="trace_id",
        metavar="QUESTION_OR_CALL_ID",
        help="Generate an HTML execution trace visualization",
    )
    parser.add_argument(
        "--add-question",
        dest="add_question",
        metavar="TEXT",
        help="Add a question to the workspace without investigating it yet",
    )
    parser.add_argument(
        "--parent",
        dest="parent_id",
        metavar="QUESTION_ID",
        help="Parent question for --add-question",
    )
    parser.add_argument(
        "--ingest",
        dest="ingest_files",
        action="append",
        metavar="FILE",
        help="Ingest a source file (can be repeated for multiple files)",
    )
    parser.add_argument(
        "--for-question",
        dest="for_question_id",
        metavar="QUESTION_ID",
        help="Extract considerations from ingested source(s) for this question",
    )
    parser.add_argument(
        "--no-trace",
        dest="no_trace",
        action="store_true",
        help="Disable execution tracing for this run",
    )
    parser.add_argument(
        "--workspace",
        dest="workspace_name",
        default="default",
        help="Project workspace name (default: 'default'). Auto-created on first use.",
    )
    parser.add_argument(
        "--list-workspaces",
        dest="list_workspaces",
        action="store_true",
        help="List all project workspaces",
    )
    parser.add_argument(
        "--batch",
        dest="batch_file",
        metavar="FILE",
        help="JSON file with a list of questions to investigate: "
        '[{"question": "...", "budget": 10}, ...]',
    )
    parser.add_argument(
        "--prod-db",
        dest="prod_db",
        action="store_true",
        help="Use production Supabase (requires SUPABASE_PROD_URL and SUPABASE_PROD_KEY)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable info-level logging to stderr",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug-level logging to stderr (very verbose)",
    )
    args = parser.parse_args()

    if args.debug:
        log_level = logging.DEBUG
    elif args.verbose:
        log_level = logging.INFO
    else:
        log_level = logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # Suppress noisy third-party loggers unless --debug
    if not args.debug:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("supabase").setLevel(logging.WARNING)
        logging.getLogger("hpack").setLevel(logging.WARNING)

    if args.no_trace:
        tracer.TRACING_ENABLED = False

    PAGES_DIR.mkdir(parents=True, exist_ok=True)

    db = await DB.create(run_id=str(uuid.uuid4()), prod=args.prod_db)

    if args.list_workspaces:
        await cmd_list_workspaces(db)
        return

    project = await db.get_or_create_project(args.workspace_name)
    db.project_id = project.id

    if args.list:
        await cmd_list(db, args.workspace_name)
        return
    elif args.trace_id:
        await cmd_trace(args.trace_id, db)
    elif args.chat_id:
        await run_chat(args.chat_id, db)
    elif args.add_question:
        await cmd_add_question(args.add_question, args.parent_id, args.budget, db)
    elif args.map_id:
        await cmd_map(args.map_id, db)
    elif args.summary_id:
        await cmd_summary(args.summary_id, db)
    elif args.continue_id:
        await cmd_continue(args.continue_id, args.budget, db)
    elif args.batch_file:
        await cmd_batch(args.batch_file, db)
    elif args.ingest_files and not args.question:
        await cmd_ingest(args.ingest_files, args.for_question_id, args.budget, db)
    elif args.question:
        await cmd_new(args.question, args.budget, db, ingest_files=args.ingest_files)
    else:
        parser.print_help()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
