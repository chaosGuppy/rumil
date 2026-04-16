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

from rumil.ab_eval import run_ab_eval
from rumil.chat import run_chat, run_continuation_chat, run_scoping_chat
from rumil.clean import run_feedback_update, run_grounding_feedback
from rumil.constants import MIN_TWOPHASE_BUDGET
from rumil.database import DB
from rumil.evaluate.runner import run_evaluation
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.orchestrators import Orchestrator, create_root_question
from rumil.report import generate_report, save_report
from rumil.settings import Settings, _settings_var, get_settings
from rumil.sources import create_source_page, run_ingest_calls
from rumil.summary import generate_summary, save_summary


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
    if path.suffix == ".json":
        if not path.exists():
            sys.exit(f"Error: question JSON file not found: {value}")
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict) or "headline" not in data:
            sys.exit('Error: JSON file must contain at least a "headline" field.')
        unknown = set(data) - {"headline", "abstract", "content"}
        if unknown:
            sys.exit(
                f"Error: unknown fields in question JSON: {', '.join(sorted(unknown))}"
            )
        return QuestionInput(
            headline=data["headline"],
            abstract=data.get("abstract", ""),
            content=data.get("content", ""),
        )
    return QuestionInput(headline=value, content=value)


NORMAL_BUDGET_DEFAULT = 10


def _default_budget(budget: int | None, fallback: int = NORMAL_BUDGET_DEFAULT) -> int:
    if budget is not None:
        return budget
    settings = get_settings()
    if settings.is_smoke_test:
        if settings.force_twophase_recurse:
            return 12
        if settings.prioritizer_variant == "two_phase":
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
    print("\nRun --chat to explore the results.")


async def cmd_evaluate(question_id: str, db: DB, *, eval_type: str = "default") -> None:

    question = await db.get_page(question_id)
    if not question:
        resolved = await db.resolve_page_id(question_id)
        if resolved:
            question = await db.get_page(resolved)
    if not question:
        print(
            f"Error: question '{question_id}' not found. Run --list to see existing questions."
        )
        sys.exit(1)

    if question.project_id and question.project_id != db.project_id:
        db.project_id = question.project_id

    frontend = get_settings().frontend_url.rstrip("/")
    print(f"\nEvaluating judgement for: {question.headline[:80]}")
    print(f"Trace: {frontend}/traces/{db.run_id}\n")

    call = await run_evaluation(question.id, db, eval_type=eval_type)
    print(f"\nEvaluation complete (call {call.id}).\n")
    _print_evaluation(call)


def _print_evaluation(call: Call) -> None:
    evaluation_text = (call.review_json or {}).get("evaluation", "")
    if evaluation_text:
        print(evaluation_text)
    elif call.result_summary:
        print(call.result_summary)
    else:
        print("(no evaluation output)")


async def cmd_ground(eval_call_id: str, db: DB, *, from_stage: int = 1) -> None:
    resolved_id = await db.resolve_call_id(eval_call_id)
    if not resolved_id:
        print(f"Error: call '{eval_call_id}' not found.")
        sys.exit(1)
    call = await db.get_call(resolved_id)
    if not call:
        print(f"Error: call '{eval_call_id}' not found.")
        sys.exit(1)
    if call.call_type != CallType.EVALUATE:
        print(
            f"Error: call '{eval_call_id}' is a {call.call_type.value} call, "
            "not an evaluation. Pass the ID of a completed evaluation call."
        )
        sys.exit(1)
    if call.status != CallStatus.COMPLETE:
        print(
            f"Error: evaluation call '{eval_call_id}' has status "
            f"'{call.status.value}'. It must be complete."
        )
        sys.exit(1)

    evaluation_text = (call.review_json or {}).get("evaluation", "")
    if not evaluation_text:
        print("Error: evaluation call has no evaluation output.")
        sys.exit(1)

    if not call.scope_page_id:
        print("Error: evaluation call has no scope question.")
        sys.exit(1)

    question = await db.get_page(call.scope_page_id)
    if not question:
        print(f"Error: scope question '{call.scope_page_id}' not found.")
        sys.exit(1)

    if question.project_id and question.project_id != db.project_id:
        db.project_id = question.project_id

    prior_checkpoints: dict | None = None
    if from_stage > 1:
        prior_checkpoints = await _load_prior_checkpoints(
            call.scope_page_id, from_stage, db
        )

    await db.create_run(
        name=f"grounding: {question.headline[:80]}",
        question_id=call.scope_page_id,
        config=get_settings().capture_config(),
    )

    frontend = get_settings().frontend_url.rstrip("/")
    print(f"\nRunning grounding feedback for: {question.headline[:80]}")
    if from_stage > 1:
        print(f"Resuming from stage {from_stage}")
    print(f"Trace: {frontend}/traces/{db.run_id}\n")

    result = await run_grounding_feedback(
        call.scope_page_id,
        evaluation_text,
        db,
        from_stage=from_stage,
        prior_checkpoints=prior_checkpoints,
    )
    print(f"\nGrounding feedback complete (call {result.id}).")
    if result.result_summary:
        print(result.result_summary)


async def cmd_feedback_update(
    eval_call_id: str, db: DB, *, investigation_budget: int | None = None
) -> None:
    resolved_id = await db.resolve_call_id(eval_call_id)
    if not resolved_id:
        print(f"Error: call '{eval_call_id}' not found.")
        sys.exit(1)
    call = await db.get_call(resolved_id)
    if not call:
        print(f"Error: call '{eval_call_id}' not found.")
        sys.exit(1)
    if call.call_type != CallType.EVALUATE:
        print(
            f"Error: call '{eval_call_id}' is a {call.call_type.value} call, "
            "not an evaluation. Pass the ID of a completed evaluation call."
        )
        sys.exit(1)
    if call.status != CallStatus.COMPLETE:
        print(
            f"Error: evaluation call '{eval_call_id}' has status "
            f"'{call.status.value}'. It must be complete."
        )
        sys.exit(1)

    evaluation_text = (call.review_json or {}).get("evaluation", "")
    if not evaluation_text:
        print("Error: evaluation call has no evaluation output.")
        sys.exit(1)

    if not call.scope_page_id:
        print("Error: evaluation call has no scope question.")
        sys.exit(1)

    question = await db.get_page(call.scope_page_id)
    if not question:
        print(f"Error: scope question '{call.scope_page_id}' not found.")
        sys.exit(1)

    if question.project_id and question.project_id != db.project_id:
        db.project_id = question.project_id

    await db.create_run(
        name=f"feedback-update: {question.headline[:80]}",
        question_id=call.scope_page_id,
        config=get_settings().capture_config(),
    )

    frontend = get_settings().frontend_url.rstrip("/")
    print(f"\nRunning feedback update for: {question.headline[:80]}")
    print(f"Trace: {frontend}/traces/{db.run_id}\n")

    if investigation_budget is not None:
        get_settings().feedback_investigation_budget = investigation_budget

    result = await run_feedback_update(
        call.scope_page_id,
        evaluation_text,
        db,
    )
    print(f"\nFeedback update complete (call {result.id}).")
    if result.result_summary:
        print(result.result_summary)


async def cmd_feedback_update_from_file(
    question_id: str,
    file_path: str,
    db: DB,
    *,
    investigation_budget: int | None = None,
) -> None:

    path = Path(file_path)
    if not path.is_file():
        print(f"Error: file '{file_path}' not found.")
        sys.exit(1)

    evaluation_text = path.read_text().strip()
    if not evaluation_text:
        print(f"Error: file '{file_path}' is empty.")
        sys.exit(1)

    resolved_id = await db.resolve_page_id(question_id)
    if not resolved_id:
        print(f"Error: question '{question_id}' not found.")
        sys.exit(1)

    question = await db.get_page(resolved_id)
    if not question:
        print(f"Error: question '{question_id}' not found.")
        sys.exit(1)

    if question.project_id and question.project_id != db.project_id:
        db.project_id = question.project_id

    await db.create_run(
        name=f"feedback-update (file): {question.headline[:80]}",
        question_id=resolved_id,
        config=get_settings().capture_config(),
    )

    frontend = get_settings().frontend_url.rstrip("/")
    print(f"\nRunning feedback update for: {question.headline[:80]}")
    print(f"Source: {file_path}")
    print(f"Trace: {frontend}/traces/{db.run_id}\n")

    if investigation_budget is not None:
        get_settings().feedback_investigation_budget = investigation_budget

    result = await run_feedback_update(
        resolved_id,
        evaluation_text,
        db,
    )
    print(f"\nFeedback update complete (call {result.id}).")
    if result.result_summary:
        print(result.result_summary)


async def _load_prior_checkpoints(question_id: str, from_stage: int, db: DB) -> dict:
    """Find the most recent grounding call for *question_id* and return its checkpoints."""

    rows = await db._execute(
        db.client.table("calls")
        .select("id, call_params")
        .eq("call_type", CallType.GROUNDING_FEEDBACK.value)
        .eq("scope_page_id", question_id)
        .is_("parent_call_id", "null")
        .order("created_at", desc=True)
        .limit(1)
    )
    if not rows.data:
        print("Error: no prior grounding call found for this question.")
        sys.exit(1)

    prior = rows.data[0]
    checkpoints = (prior.get("call_params") or {}).get("checkpoints", {})

    required_keys = {
        2: ["tasks"],
        3: ["tasks", "findings"],
        4: ["tasks", "findings", "update_plan"],
        5: ["tasks", "findings", "update_plan"],
    }
    missing = [k for k in required_keys.get(from_stage, []) if k not in checkpoints]
    if missing:
        print(
            f"Error: prior grounding call {prior['id'][:8]} is missing "
            f"checkpoint data for: {', '.join(missing)}. "
            f"Cannot resume from stage {from_stage}."
        )
        sys.exit(1)

    print(f"Loaded checkpoints from prior call {prior['id'][:8]}")
    return checkpoints


async def cmd_show_evaluation(call_id: str, db: DB) -> None:

    call = await db.get_call(call_id)
    if not call:
        print(f"Error: call '{call_id}' not found.")
        sys.exit(1)

    if call.call_type != CallType.EVALUATE:
        print(
            f"Error: call '{call_id}' is a {call.call_type.value} call, not an evaluation."
        )
        sys.exit(1)

    scope = await db.get_page(call.scope_page_id) if call.scope_page_id else None
    if scope:
        print(f"Evaluation for: {scope.headline[:80]}")
    print(f"Call: {call.id[:8]}  Status: {call.status.value}\n")
    _print_evaluation(call)


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


async def cmd_report(
    question_id: str,
    db: DB,
    max_depth: int = 4,
) -> None:
    question = await db.get_page(question_id)
    if not question:
        print(
            f"Error: question '{question_id}' not found. Run --list to see existing questions."
        )
        sys.exit(1)

    print(f"\nGenerating report for: {question.headline[:80]}")
    print(
        "(This will use multiple LLM calls but does not count against research budget)\n"
    )

    report_text = await generate_report(question_id, db, max_depth=max_depth)
    path = save_report(report_text, question.headline)

    print(report_text)
    print(f"\n---\nReport saved to: {path}")


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
        q.headline,
        db,
        abstract=q.abstract,
        content=q.content,
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
        q.headline,
        db,
        abstract=q.abstract,
        content=q.content,
    )
    await db.create_ab_run(ab_run_id, name or q.headline, question_id)

    frontend = get_settings().frontend_url.rstrip("/")
    print(f"\nAB test: {ab_run_id}")
    print(f"Headline: {q.headline}")
    print(f"Budget per arm: {budget}")
    print(f"Trace: {frontend}/ab-traces/{ab_run_id}")

    async def run_arm(arm_label: str, env_file: str) -> None:
        arm_settings = Settings.from_env_files(".env", env_file)
        if get_settings().is_smoke_test:
            arm_settings.rumil_smoke_test = "1"
        if get_settings().is_prod_db:
            arm_settings.use_prod_db = "1"
        if not get_settings().tracing_enabled:
            arm_settings.tracing_enabled = False
        _settings_var.set(arm_settings)

        arm_db = await DB.create(
            run_id=str(uuid.uuid4()),
            prod=arm_settings.is_prod_db,
            client=db.client,
            project_id=db.project_id,
            staged=True,
            ab_run_id=ab_run_id,
        )
        config = arm_settings.capture_config()
        await arm_db.create_run(
            name=f"{name or q.headline[:100]} (arm {arm_label})",
            question_id=question_id,
            config=config,
            ab_arm=arm_label,
        )
        await arm_db.init_budget(budget)
        await Orchestrator(arm_db).run(question_id)
        total, used = await arm_db.get_budget()
        print(f"\nArm {arm_label} complete: {used}/{total} budget used")

    async with asyncio.TaskGroup() as tg:
        tg.create_task(run_arm("a", ".a.env"))
        tg.create_task(run_arm("b", ".b.env"))

    print(f"\nAB test complete: {frontend}/ab-traces/{ab_run_id}")


async def cmd_ab_eval(
    run_id_a: str,
    run_id_b: str,
    db: DB,
) -> None:
    """Run A/B evaluation agents comparing two staged runs."""

    await run_ab_eval(run_id_a, run_id_b, db)


async def cmd_scope(
    question_text: str,
    budget: int | None,
    db: DB,
    name: str = "",
    ingest_files: list[str] | None = None,
) -> None:
    effective_budget = _default_budget(budget)
    await db.create_run(
        name=name or f"scope: {question_text[:100]}",
        question_id="",
        config=get_settings().capture_config(),
    )
    # Create source pages up front (no extraction yet)
    source_pages: list[Page] = []
    if ingest_files:
        for filepath in ingest_files:
            page = await create_source_page(filepath, db)
            if page:
                source_pages.append(page)

    question_id = await run_scoping_chat(
        question_text,
        db,
        effective_budget,
        source_pages=source_pages,
    )
    if question_id:
        await _print_summary(db)


async def cmd_continue(
    question_id: str,
    additional_budget: int | None,
    db: DB,
    name: str = "",
    ingest_files: list[str] | None = None,
    chat_first: bool = False,
) -> None:
    additional_budget = _default_budget(additional_budget)
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

    if question.project_id and question.project_id != db.project_id:
        db.project_id = question.project_id

    counts = await db.count_pages_for_question(question_id)
    await db.init_budget(additional_budget)
    await db.create_run(
        name=name or f"continue: {question.headline[:100]}",
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

    if chat_first:
        source_pages: list[Page] = []
        if ingest_files:
            for filepath in ingest_files:
                page = await create_source_page(filepath, db)
                if page:
                    source_pages.append(page)
        await run_continuation_chat(
            question_id,
            db,
            additional_budget,
            source_pages=source_pages,
        )
        await _print_summary(db)
        return

    ingested_source_names: list[str] = []
    existing_claim_ids: set[str] = set()
    if ingest_files:
        existing_claim_ids = {
            p.id for p in await db.get_pages(page_type=PageType.CLAIM)
        }
        source_pages = []
        for filepath in ingest_files:
            page = await create_source_page(filepath, db)
            if page:
                source_pages.append(page)
                ingested_source_names.append(page.headline)
        if source_pages:
            print(f"\nIngesting {len(source_pages)} source file(s)...")
            await run_ingest_calls(source_pages, question_id, db)

    orch = Orchestrator(db)
    if ingested_source_names:
        all_claims = await db.get_pages(page_type=PageType.CLAIM)
        ingested_claims = [p for p in all_claims if p.id not in existing_claim_ids]
        claim_lines = [f"  - `{p.id}` {p.headline}" for p in ingested_claims]
        sources = ", ".join(ingested_source_names)
        orch.ingest_hint = (
            f"New material was just ingested from: {sources}. "
            "The following considerations were extracted:\n"
            + "\n".join(claim_lines)
            + "\n\nNot every extracted claim is necessarily important — use your "
            "judgement about which ones are worth investigating further. But the "
            "user specifically provided this source, so the material as a whole "
            "may deserve more attention than scores alone suggest. "
            "The View for this question has NOT yet been updated to reflect this "
            "new material — ingested considerations are not yet factored into "
            "View scores or item selection."
        )
    await orch.run(question_id)
    await _print_summary(db)


async def _print_summary(db: DB) -> None:
    total, used = await db.get_budget()
    print(f"\nBudget used: {used}/{total} calls")
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
        "question",
        nargs="?",
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
        "--report",
        dest="report_id",
        metavar="QUESTION_ID",
        help="Generate a multi-section research report for a question",
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
        "--evaluate",
        dest="evaluate_id",
        metavar="QUESTION_ID",
        help="Evaluate the judgement quality for a question",
    )
    parser.add_argument(
        "--eval-type",
        dest="eval_type",
        default="default",
        help="Evaluation prompt type (default: default). Options: default, grounding, feedback",
    )
    parser.add_argument(
        "--ground",
        dest="ground_call_id",
        metavar="EVAL_CALL_ID",
        help="Run grounding feedback pipeline on a completed evaluation call",
    )
    parser.add_argument(
        "--from-stage",
        dest="from_stage",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Resume grounding from stage N (1-6), reusing checkpointed "
            "outputs from the most recent prior grounding run"
        ),
    )
    parser.add_argument(
        "--feedback",
        dest="feedback_call_id",
        metavar="EVAL_CALL_ID",
        help="Run feedback update pipeline on a completed feedback evaluation call",
    )
    parser.add_argument(
        "--feedback-file",
        dest="feedback_file",
        nargs=2,
        metavar=("QUESTION_ID", "FILE_PATH"),
        help="Run feedback update pipeline using feedback text from a file",
    )
    parser.add_argument(
        "--show-evaluation",
        dest="show_evaluation_id",
        metavar="CALL_ID",
        help="Display the full output of a completed evaluation call",
    )
    parser.add_argument(
        "--chat",
        dest="chat_id",
        metavar="QUESTION_ID",
        help="Chat interactively about the research on a question",
    )
    parser.add_argument(
        "--scope",
        dest="scope_question",
        metavar="QUESTION_TEXT",
        help="Start a scoping chat to refine a question before investigation",
    )
    parser.add_argument(
        "--chat-first",
        dest="chat_first",
        action="store_true",
        help="Enter continuation chat before resuming investigation (use with --continue)",
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
        metavar="FILE_OR_URL",
        help="Ingest a source file or URL (can be repeated)",
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
        "--available-moves",
        dest="available_moves",
        default=None,
        help="Available-moves preset name (default: 'default'). Controls which moves are available per call type.",
    )
    parser.add_argument(
        "--available-calls",
        dest="available_calls",
        default=None,
        help="Available-calls preset name (default: 'default'). Controls which scout/dispatch types the two-phase orchestrator uses.",
    )
    parser.add_argument(
        "--ingest-num-claims",
        dest="ingest_num_claims",
        type=int,
        default=None,
        help=(
            "Target number of considerations to extract per ingest call "
            "(default: 4). The prompt uses 'approximately', so the model "
            "will still apply quality-over-quantity judgement."
        ),
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
        "--staged",
        action="store_true",
        help="Run in staged mode: page mutations are recorded as events instead "
        "of modifying the database directly",
    )
    parser.add_argument(
        "--run-id-file",
        dest="run_id_file",
        metavar="PATH",
        help="Write the run_id to this file after DB creation (for scripted capture)",
    )
    parser.add_argument(
        "--env-file",
        dest="env_file",
        metavar="PATH",
        help="Load settings from this env file in addition to .env",
    )
    parser.add_argument(
        "--ab-eval",
        dest="ab_eval_ids",
        nargs=2,
        metavar=("RUN_ID_A", "RUN_ID_B"),
        help="Run A/B evaluation agents comparing two staged runs",
    )
    parser.add_argument(
        "--stage-run",
        dest="stage_run_id",
        metavar="RUN_ID",
        help="Retroactively stage a completed non-staged run, hiding its "
        "effects from baseline readers",
    )
    parser.add_argument(
        "--commit-run",
        dest="commit_run_id",
        metavar="RUN_ID",
        help="Commit a staged run, making its effects visible to all readers",
    )
    parser.add_argument(
        "-q",
        "--quiet",
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

    if args.env_file:
        _settings_var.set(Settings.from_env_files(".env", args.env_file))

    if args.available_moves is not None:
        get_settings().available_moves = args.available_moves
    if args.available_calls is not None:
        get_settings().available_calls = args.available_calls
    if args.ingest_num_claims is not None:
        get_settings().ingest_num_claims = args.ingest_num_claims
    if args.smoke_test:
        get_settings().rumil_smoke_test = "1"
    if args.prod_db:
        get_settings().use_prod_db = "1"
    if args.no_trace:
        get_settings().tracing_enabled = False
    if args.force_twophase_recurse:
        get_settings().force_twophase_recurse = True

    db = await DB.create(
        run_id=str(uuid.uuid4()), prod=args.prod_db, staged=args.staged
    )

    if args.run_id_file:
        Path(args.run_id_file).write_text(db.run_id, encoding="utf-8")

    if args.list_workspaces:
        await cmd_list_workspaces(db)
        return

    project = await db.get_or_create_project(args.workspace_name)
    db.project_id = project.id

    if args.stage_run_id:
        await db.stage_run(args.stage_run_id)
        print(f"Run {args.stage_run_id} has been staged.")
        return

    if args.commit_run_id:
        await db.commit_staged_run(args.commit_run_id)
        print(f"Run {args.commit_run_id} has been committed.")
        return

    if args.ab_eval_ids:
        await cmd_ab_eval(args.ab_eval_ids[0], args.ab_eval_ids[1], db)
        return

    if args.list:
        await cmd_list(db, args.workspace_name)
        return
    elif args.evaluate_id:
        await cmd_evaluate(args.evaluate_id, db, eval_type=args.eval_type)
        return
    elif args.ground_call_id:
        await cmd_ground(args.ground_call_id, db, from_stage=args.from_stage)
        return
    elif args.feedback_call_id:
        await cmd_feedback_update(
            args.feedback_call_id, db, investigation_budget=args.budget
        )
        return
    elif args.feedback_file:
        await cmd_feedback_update_from_file(
            args.feedback_file[0],
            args.feedback_file[1],
            db,
            investigation_budget=args.budget,
        )
        return
    elif args.show_evaluation_id:
        await cmd_show_evaluation(args.show_evaluation_id, db)
        return
    elif args.scope_question:
        await cmd_scope(
            args.scope_question,
            args.budget,
            db,
            name=args.run_name,
            ingest_files=args.ingest_files,
        )
    elif args.chat_id:
        await run_chat(args.chat_id, db)
    elif args.add_question:
        q = parse_question_input(args.add_question)
        await cmd_add_question(q, args.parent_id, args.budget, db)
    elif args.summary_id:
        await cmd_summary(
            args.summary_id,
            db,
            max_depth=args.max_depth,
            summary_cutoff=args.summarize_after_depth,
        )
    elif args.report_id:
        await cmd_report(
            args.report_id,
            db,
            max_depth=args.max_depth,
        )
    elif args.continue_id:
        await cmd_continue(
            args.continue_id,
            args.budget,
            db,
            name=args.run_name,
            ingest_files=args.ingest_files,
            chat_first=args.chat_first,
        )
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
            q,
            args.budget,
            db,
            ingest_files=args.ingest_files,
            name=args.run_name,
        )
    else:
        parser.print_help()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
