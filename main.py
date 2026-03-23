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
import dataclasses
import json
import logging
import sys
import uuid
from pathlib import Path

from rumil.database import DB
from rumil.models import Page, PageLayer, PageLink, PageType, LinkType, Workspace
from rumil.constants import MIN_TWOPHASE_BUDGET
from rumil.orchestrator import Orchestrator, create_root_question, run_concept_session
from rumil.sources import create_source_page, run_ingest_calls
from rumil.chat import run_chat
from rumil.mapper import generate_map
from rumil.summary import generate_summary, save_summary
from rumil.settings import Settings, get_settings, _settings_var

PAGES_DIR = Path(__file__).parent / "pages"


@dataclasses.dataclass
class QuestionInput:
    headline: str
    abstract: str = ""
    content: str = ""


def parse_question_input(value: str) -> QuestionInput:
    """Parse a question from plain text or a JSON file path.

    If *value* ends with ``.json`` and the file exists, it is read as JSON with
    required ``headline`` and optional ``abstract`` / ``content`` fields.
    Otherwise *value* is treated as plain headline text (used for both headline
    and content, matching legacy behaviour).
    """
    path = Path(value)
    if path.suffix == '.json' and path.exists():
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict) or 'headline' not in data:
            sys.exit('Error: JSON file must contain at least a "headline" field.')
        unknown = set(data) - {'headline', 'abstract', 'content'}
        if unknown:
            sys.exit(f'Error: unknown fields in question JSON: {", ".join(sorted(unknown))}')
        return QuestionInput(
            headline=data['headline'],
            abstract=data.get('abstract', ''),
            content=data.get('content', ''),
        )
    return QuestionInput(headline=value, content=value)

NORMAL_BUDGET_DEFAULT = 10


def _default_budget(budget: int | None, fallback: int = NORMAL_BUDGET_DEFAULT) -> int:
    if budget is not None:
        return budget
    settings = get_settings()
    if settings.is_smoke_test:
        if settings.prioritizer_variant == 'two_phase':
            return MIN_TWOPHASE_BUDGET
        return 1
    return fallback


async def cmd_add_question(
    q: QuestionInput, parent_id: str | None, budget: int | None, db: DB
) -> None:
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=q.content or q.abstract or q.headline,
        headline=q.headline,
        abstract=q.abstract,
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
            print(f"\nAdded as sub-question of: {parent.headline[:70]}")

    print(f"\nQuestion added: {page.id}")
    print(f"Headline:       {q.headline}")

    effective_budget = _default_budget(budget, fallback=5)
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


async def cmd_ingest(
    ingest_files: list[str], for_question_id: str | None, budget: int | None, db: DB
) -> None:
    # Create source pages first (always free)
    source_pages = []
    for filepath in ingest_files:
        page = await create_source_page(filepath, db)
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

    frontend = get_settings().frontend_url.rstrip("/")
    print(f"\nExtracting considerations for: {question.headline[:80]}")
    print(f"Budget: {effective_budget} call{'s' if effective_budget != 1 else ''}")
    print(f"Trace:  {frontend}/traces/{db.run_id}\n")
    await db.init_budget(effective_budget)
    made = await run_ingest_calls(source_pages, for_question_id, db)
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
    print(f"\nGenerating map for: {question.headline[:80]}")
    path = await generate_map(question_id, db)
    print(f"Map saved to: {path}")
    print("Open that file in your browser to view it.")


async def cmd_summary(
    question_id: str,
    db: DB,
    max_depth: int = 4,
    summary_cutoff: int | None = None,
) -> None:
    question = await db.get_page(question_id)
    if not question:
        print(
            f"Error: question '{question_id}' not found. Run --list to see existing questions."
        )
        sys.exit(1)

    print(f"\nGenerating summary for: {question.headline[:80]}")
    print("(This will use one LLM call but does not count against research budget)\n")

    summary_text = await generate_summary(
        question_id, db, max_depth=max_depth, summary_cutoff=summary_cutoff
    )
    path = save_summary(summary_text, question.headline)

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
        truncated = q.headline[:55] + "…" if len(q.headline) > 55 else q.headline
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
    q: QuestionInput,
    budget: int | None,
    db: DB,
    ingest_files: list[str] | None = None,
    name: str = "",
) -> None:
    budget = _default_budget(budget)
    await db.init_budget(budget)
    question_id = await create_root_question(
        q.headline, db, abstract=q.abstract, content=q.content,
    )
    await db.create_run(
        name=name or q.headline,
        question_id=question_id,
        config=get_settings().capture_config(),
    )

    frontend = get_settings().frontend_url.rstrip("/")
    print(f"\nNew question: {question_id}")
    print(f"Headline:     {q.headline}")
    print(f"Budget:       {budget} research calls")
    print(f"Trace:        {frontend}/traces/{db.run_id}")

    if ingest_files:
        source_pages = []
        for filepath in ingest_files:
            page = await create_source_page(filepath, db)
            if page:
                source_pages.append(page)
        if source_pages:
            print(f"\nIngesting {len(source_pages)} source file(s)...")
            await run_ingest_calls(source_pages, question_id, db)

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
        q = parse_question_input(entry["question"])
        await cmd_new(q, budget, db, ingest_files=entry.get("ingest"))

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


async def cmd_ab(
    q: QuestionInput,
    budget: int | None,
    db: DB,
    name: str = "",
) -> None:
    """Run an A/B test: two concurrent investigations with different configs."""
    ab_run_id = str(uuid.uuid4())
    budget = _default_budget(budget)

    question_id = await create_root_question(
        q.headline, db, abstract=q.abstract, content=q.content,
    )
    await db.create_ab_run(ab_run_id, name or q.headline, question_id)

    frontend = get_settings().frontend_url.rstrip("/")
    print(f'\nAB test: {ab_run_id}')
    print(f'Headline: {q.headline}')
    print(f'Budget per arm: {budget}')
    print(f'Trace: {frontend}/ab-traces/{ab_run_id}')

    async def run_arm(arm_label: str, env_file: str) -> None:
        arm_settings = Settings.from_env_files('.env', env_file)
        if get_settings().is_smoke_test:
            arm_settings.rumil_smoke_test = '1'
        if get_settings().is_prod_db:
            arm_settings.use_prod_db = '1'
        if not get_settings().tracing_enabled:
            arm_settings.tracing_enabled = False
        _settings_var.set(arm_settings)

        arm_db = await DB.create(
            run_id=str(uuid.uuid4()),
            prod=arm_settings.is_prod_db,
            client=db.client,
            project_id=db.project_id,
            ab_run_id=ab_run_id,
        )
        config = arm_settings.capture_config()
        await arm_db.create_run(
            name=f'{name or q.headline[:100]} (arm {arm_label})',
            question_id=question_id,
            config=config,
            ab_arm=arm_label,
        )
        await arm_db.init_budget(budget)
        await Orchestrator(arm_db).run(question_id)
        total, used = await arm_db.get_budget()
        print(f'\nArm {arm_label} complete: {used}/{total} budget used')

    async with asyncio.TaskGroup() as tg:
        tg.create_task(run_arm('a', '.a.env'))
        tg.create_task(run_arm('b', '.b.env'))

    print(f'\nAB test complete: {frontend}/ab-traces/{ab_run_id}')


async def cmd_continue(
    question_id: str,
    additional_budget: int | None,
    db: DB,
    name: str = "",
) -> None:
    additional_budget = additional_budget if additional_budget is not None else (
        1 if get_settings().is_smoke_test else 10
    )
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
    await db.create_run(
        name=name or f'continue: {question.headline[:100]}',
        question_id=question_id,
        config=get_settings().capture_config(),
    )

    frontend = get_settings().frontend_url.rstrip("/")
    print(f"\nContinuing investigation of: {question.headline[:80]}")
    print(f"Question ID:  {question_id}")
    print(
        f"Existing:     {counts['considerations']} considerations, {counts['judgements']} judgements"
    )
    print(f"Budget:       {additional_budget} research calls")
    print(f"Trace:        {frontend}/traces/{db.run_id}")

    await Orchestrator(db).run(question_id)
    await _print_summary(db)


async def cmd_concepts(question_id: str, db: DB) -> None:
    question = await db.get_page(question_id)
    if not question:
        print(
            f"Error: question '{question_id}' not found. Run --list to see existing questions."
        )
        sys.exit(1)
    print(f"\nRunning concept session for: {question.headline[:80]}")
    print(f"Question ID: {question_id}")
    print("(Concept generation does not consume research budget)\n")
    await run_concept_session(question_id, db)
    registry = await db.get_concept_registry()
    promoted = [p for p in registry if p.extra.get("promoted")]
    print(f"\nConcept session complete.")
    print(f"Registry: {len(registry)} total proposals, {len(promoted)} promoted.")
    if promoted:
        print("\nPromoted concepts:")
        for p in promoted:
            print(f"  {p.id[:8]}  {p.headline}")


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
    parser.add_argument(
        "question", nargs="?",
        help="Question to investigate (new run). Plain text or path to a .json file "
        "with headline (required), abstract, and content fields.",
    )
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
        "--max-depth",
        type=int,
        default=4,
        help="Maximum tree depth for --summary (default: 4)",
    )
    parser.add_argument(
        "--summarize-after-depth",
        type=int,
        default=None,
        help=(
            "Depth at which --summary switches from full content to page "
            "summaries only (default: max-depth // 2)"
        ),
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
        "--concepts",
        dest="concepts_id",
        metavar="QUESTION_ID",
        help="Run a concept-generation session for a question",
    )
    parser.add_argument(
        "--add-question",
        dest="add_question",
        metavar="TEXT_OR_JSON",
        help="Add a question to the workspace without investigating it yet. "
        "Pass plain text or a .json file with headline, abstract, content fields.",
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
        "--ab",
        dest="ab_test",
        action="store_true",
        help="Run an A/B test with two arms (requires .a.env and .b.env)",
    )
    parser.add_argument(
        "--name",
        dest="run_name",
        default="",
        help="Optional name for this run (defaults to question text)",
    )
    parser.add_argument(
        "--smoke-test",
        dest="smoke_test",
        action="store_true",
        help="Smoke-test mode: use Haiku, fewer rounds, lower budget defaults",
    )
    parser.add_argument(
        "--force-twophase-recurse",
        dest="force_twophase_recurse",
        action="store_true",
        help="Force the two-phase orchestrator to dispatch two recurse calls",
    )
    parser.add_argument(
        "--prod",
        dest="prod_db",
        action="store_true",
        help="Use production Supabase (requires SUPABASE_PROD_URL and SUPABASE_PROD_KEY)",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Suppress info-level logging (only show warnings and errors)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug-level logging to stderr (very verbose)",
    )
    args = parser.parse_args()

    if args.debug:
        log_level = logging.DEBUG
    elif args.quiet:
        log_level = logging.WARNING
    else:
        log_level = logging.INFO
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("rumil").setLevel(log_level)

    if args.smoke_test:
        get_settings().rumil_smoke_test = "1"
    if args.prod_db:
        get_settings().use_prod_db = "1"
    if args.no_trace:
        get_settings().tracing_enabled = False
    if args.force_twophase_recurse:
        get_settings().force_twophase_recurse = True

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
    elif args.concepts_id:
        await cmd_concepts(args.concepts_id, db)
    elif args.chat_id:
        await run_chat(args.chat_id, db)
    elif args.add_question:
        q = parse_question_input(args.add_question)
        await cmd_add_question(q, args.parent_id, args.budget, db)
    elif args.map_id:
        await cmd_map(args.map_id, db)
    elif args.summary_id:
        await cmd_summary(
            args.summary_id, db,
            max_depth=args.max_depth,
            summary_cutoff=args.summarize_after_depth,
        )
    elif args.continue_id:
        await cmd_continue(args.continue_id, args.budget, db, name=args.run_name)
    elif args.batch_file:
        await cmd_batch(args.batch_file, db)
    elif args.ingest_files and not args.question:
        await cmd_ingest(args.ingest_files, args.for_question_id, args.budget, db)
    elif args.question and args.ab_test:
        q = parse_question_input(args.question)
        await cmd_ab(q, args.budget, db, name=args.run_name)
    elif args.question:
        q = parse_question_input(args.question)
        await cmd_new(
            q, args.budget, db,
            ingest_files=args.ingest_files, name=args.run_name,
        )
    else:
        parser.print_help()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
