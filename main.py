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
import io
import json
import logging
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from rumil.ab_eval import run_ab_eval
from rumil.chat import run_chat, run_continuation_chat, run_scoping_chat
from rumil.clean import run_feedback_update, run_grounding_feedback
from rumil.cli_client import submit_remote_orchestrator_run
from rumil.constants import MIN_TWOPHASE_BUDGET
from rumil.database import DB
from rumil.evaluate.runner import run_evaluation
from rumil.memos import render_scan_summary, save_memo_scan, scan_for_memos
from rumil.memos_to_artefacts import (
    draft_memos_from_scan,
    generate_memo_summary,
    load_scan_from_path,
    save_memo_summary,
)
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
from rumil.run_eval import run_run_eval
from rumil.run_eval.agents import EVAL_AGENTS, EvalAgentSpec
from rumil.self_improve import run_self_improvement, save_self_improvement
from rumil.settings import Settings, _settings_var, get_settings
from rumil.sources import create_source_page, run_ingest_calls
from rumil.summary import generate_summary, save_summary
from rumil.tracing import get_langfuse

log = logging.getLogger("rumil.cli")


def _reconfigure_stdout_utf8() -> None:
    """Reconfigure stdout to UTF-8 with replacement on Windows consoles.

    LLM output regularly contains characters outside the cp1252 default
    Windows codepage (em-dashes, arrows, smart quotes). Without this, a
    print() of such content raises UnicodeEncodeError. Safe to call
    repeatedly. No-op when stdout isn't a TextIOWrapper (e.g. captured
    in tests).
    """
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _maybe_log_langfuse_session(db: DB) -> None:
    if get_langfuse() is None:
        return
    settings = get_settings()
    lf_base = settings.langfuse_base_url.rstrip("/")
    log.info("Langfuse: %s/sessions?sessionId=%s", lf_base, db.run_id)


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
            sys.exit(f"Error: unknown fields in question JSON: {', '.join(sorted(unknown))}")
        return QuestionInput(
            headline=data["headline"],
            abstract=data.get("abstract", ""),
            content=data.get("content", ""),
        )
    return QuestionInput(headline=value, content=value)


NORMAL_BUDGET_DEFAULT = 10


_NON_ORCHESTRATOR_FLAGS: tuple[str, ...] = (
    "list",
    "list_workspaces",
    "evaluate_id",
    "ground_call_id",
    "feedback_call_id",
    "feedback_file",
    "show_evaluation_id",
    "scope_question",
    "chat_id",
    "add_question",
    "summary_id",
    "self_improve_id",
    "report_id",
    "scan_memos_id",
    "draft_memos_path",
    "batch_file",
    "run_eval_id",
    "ab_eval_ids",
    "stage_run_id",
    "commit_run_id",
)

# A few flags use nargs="?" with const="__auto__" to mean "do this step after
# the orchestrator finishes" rather than "this is the standalone mode". An
# auto value should NOT mark the run as non-orchestrator — the orchestrator
# is still the primary action.
_AUTO_AWARE_FLAGS = frozenset({"summary_id", "self_improve_id"})
_AUTO_VALUE = "__auto__"


def _is_non_orchestrator_mode(args: argparse.Namespace) -> bool:
    """True if the CLI was invoked in any mode other than `question --budget N`."""
    for flag in _NON_ORCHESTRATOR_FLAGS:
        value = getattr(args, flag, None)
        if not value:
            continue
        if flag in _AUTO_AWARE_FLAGS and value == _AUTO_VALUE:
            continue
        return True
    return bool(getattr(args, "ingest_files", None) and not getattr(args, "question", None))


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
            log.warning("parent '%s' not found — question created without parent link.", parent_id)
        else:
            link = PageLink(
                from_page_id=parent_id,
                to_page_id=page.id,
                link_type=LinkType.CHILD_QUESTION,
                reasoning="Manually added sub-question",
            )
            await db.save_link(link)
            log.info("Added as sub-question of: %s", parent.headline[:70])

    log.info("Question added: %s", page.id)
    log.info("Headline: %s", q.headline)

    effective_budget = _default_budget(budget, fallback=5)
    if effective_budget > 0:
        log.info(
            "Budget: %d research call%s",
            effective_budget,
            "s" if effective_budget != 1 else "",
        )
        await db.init_budget(effective_budget)
        await Orchestrator(db).run(page.id)
        await _print_summary(db)
    else:
        log.info("To investigate it later: python main.py --continue %s --budget N", page.id)


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
        log.info("Sources stored. Use --for-question QUESTION_ID to extract considerations.")
        log.info("To investigate later: python main.py --ingest FILE --for-question ID --budget N")
        return

    question = await db.get_page(for_question_id)
    if not question:
        log.error("question '%s' not found. Run --list to see existing questions.", for_question_id)
        return

    effective_budget = len(source_pages) if budget is None else budget
    if effective_budget == 0:
        log.info("Sources stored (--budget 0, no extraction).")
        return

    frontend = get_settings().frontend_url.rstrip("/")
    log.info("Extracting considerations for: %s", question.headline[:80])
    log.info("Budget: %d call%s", effective_budget, "s" if effective_budget != 1 else "")
    log.info("Trace: %s/traces/%s", frontend, db.run_id)
    _maybe_log_langfuse_session(db)
    await db.init_budget(effective_budget)
    made = await run_ingest_calls(source_pages, for_question_id, db)
    total, used = await db.get_budget()
    log.info("Ingest complete. %d extraction call%s made.", made, "s" if made != 1 else "")
    log.info("Budget used: %d/%d", used, total)
    log.info("Run --chat to explore the results.")


async def cmd_evaluate(question_id: str, db: DB, *, eval_type: str = "default") -> None:

    question = await db.get_page(question_id)
    if not question:
        resolved = await db.resolve_page_id(question_id)
        if resolved:
            question = await db.get_page(resolved)
    if not question:
        log.error("question '%s' not found. Run --list to see existing questions.", question_id)
        sys.exit(1)

    if question.project_id and question.project_id != db.project_id:
        db.project_id = question.project_id

    frontend = get_settings().frontend_url.rstrip("/")
    log.info("Evaluating judgement for: %s", question.headline[:80])
    log.info("Trace: %s/traces/%s", frontend, db.run_id)
    _maybe_log_langfuse_session(db)

    call = await run_evaluation(question.id, db, eval_type=eval_type)
    log.info("Evaluation complete (call %s).", call.id)
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
        log.error("call '%s' not found.", eval_call_id)
        sys.exit(1)
    call = await db.get_call(resolved_id)
    if not call:
        log.error("call '%s' not found.", eval_call_id)
        sys.exit(1)
    if call.call_type != CallType.EVALUATE:
        log.error(
            "call '%s' is a %s call, not an evaluation. "
            "Pass the ID of a completed evaluation call.",
            eval_call_id,
            call.call_type.value,
        )
        sys.exit(1)
    if call.status != CallStatus.COMPLETE:
        log.error(
            "evaluation call '%s' has status '%s'. It must be complete.",
            eval_call_id,
            call.status.value,
        )
        sys.exit(1)

    evaluation_text = (call.review_json or {}).get("evaluation", "")
    if not evaluation_text:
        log.error("evaluation call has no evaluation output.")
        sys.exit(1)

    if not call.scope_page_id:
        log.error("evaluation call has no scope question.")
        sys.exit(1)

    question = await db.get_page(call.scope_page_id)
    if not question:
        log.error("scope question '%s' not found.", call.scope_page_id)
        sys.exit(1)

    if question.project_id and question.project_id != db.project_id:
        db.project_id = question.project_id

    prior_checkpoints: dict | None = None
    if from_stage > 1:
        prior_checkpoints = await _load_prior_checkpoints(call.scope_page_id, from_stage, db)

    await db.create_run(
        name=f"grounding: {question.headline[:80]}",
        question_id=call.scope_page_id,
        config=get_settings().capture_config(),
    )

    frontend = get_settings().frontend_url.rstrip("/")
    log.info("Running grounding feedback for: %s", question.headline[:80])
    if from_stage > 1:
        log.info("Resuming from stage %d", from_stage)
    log.info("Trace: %s/traces/%s", frontend, db.run_id)
    _maybe_log_langfuse_session(db)

    result = await run_grounding_feedback(
        call.scope_page_id,
        evaluation_text,
        db,
        from_stage=from_stage,
        prior_checkpoints=prior_checkpoints,
    )
    log.info("Grounding feedback complete (call %s).", result.id)
    if result.result_summary:
        print(result.result_summary)


async def cmd_feedback_update(
    eval_call_id: str, db: DB, *, investigation_budget: int | None = None
) -> None:
    resolved_id = await db.resolve_call_id(eval_call_id)
    if not resolved_id:
        log.error("call '%s' not found.", eval_call_id)
        sys.exit(1)
    call = await db.get_call(resolved_id)
    if not call:
        log.error("call '%s' not found.", eval_call_id)
        sys.exit(1)
    if call.call_type != CallType.EVALUATE:
        log.error(
            "call '%s' is a %s call, not an evaluation. "
            "Pass the ID of a completed evaluation call.",
            eval_call_id,
            call.call_type.value,
        )
        sys.exit(1)
    if call.status != CallStatus.COMPLETE:
        log.error(
            "evaluation call '%s' has status '%s'. It must be complete.",
            eval_call_id,
            call.status.value,
        )
        sys.exit(1)

    evaluation_text = (call.review_json or {}).get("evaluation", "")
    if not evaluation_text:
        log.error("evaluation call has no evaluation output.")
        sys.exit(1)

    if not call.scope_page_id:
        log.error("evaluation call has no scope question.")
        sys.exit(1)

    question = await db.get_page(call.scope_page_id)
    if not question:
        log.error("scope question '%s' not found.", call.scope_page_id)
        sys.exit(1)

    if question.project_id and question.project_id != db.project_id:
        db.project_id = question.project_id

    await db.create_run(
        name=f"feedback-update: {question.headline[:80]}",
        question_id=call.scope_page_id,
        config=get_settings().capture_config(),
    )

    frontend = get_settings().frontend_url.rstrip("/")
    log.info("Running feedback update for: %s", question.headline[:80])
    log.info("Trace: %s/traces/%s", frontend, db.run_id)
    _maybe_log_langfuse_session(db)

    if investigation_budget is not None:
        get_settings().feedback_investigation_budget = investigation_budget

    result = await run_feedback_update(
        call.scope_page_id,
        evaluation_text,
        db,
    )
    log.info("Feedback update complete (call %s).", result.id)
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
        log.error("file '%s' not found.", file_path)
        sys.exit(1)

    evaluation_text = path.read_text().strip()
    if not evaluation_text:
        log.error("file '%s' is empty.", file_path)
        sys.exit(1)

    resolved_id = await db.resolve_page_id(question_id)
    if not resolved_id:
        log.error("question '%s' not found.", question_id)
        sys.exit(1)

    question = await db.get_page(resolved_id)
    if not question:
        log.error("question '%s' not found.", question_id)
        sys.exit(1)

    if question.project_id and question.project_id != db.project_id:
        db.project_id = question.project_id

    await db.create_run(
        name=f"feedback-update (file): {question.headline[:80]}",
        question_id=resolved_id,
        config=get_settings().capture_config(),
    )

    frontend = get_settings().frontend_url.rstrip("/")
    log.info("Running feedback update for: %s", question.headline[:80])
    log.info("Source: %s", file_path)
    log.info("Trace: %s/traces/%s", frontend, db.run_id)
    _maybe_log_langfuse_session(db)

    if investigation_budget is not None:
        get_settings().feedback_investigation_budget = investigation_budget

    result = await run_feedback_update(
        resolved_id,
        evaluation_text,
        db,
    )
    log.info("Feedback update complete (call %s).", result.id)
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
        log.error("no prior grounding call found for this question.")
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
        log.error(
            "prior grounding call %s is missing checkpoint data for: %s. "
            "Cannot resume from stage %d.",
            prior["id"][:8],
            ", ".join(missing),
            from_stage,
        )
        sys.exit(1)

    log.info("Loaded checkpoints from prior call %s", prior["id"][:8])
    return checkpoints


async def cmd_show_evaluation(call_id: str, db: DB) -> None:
    call = await db.get_call(call_id)
    if not call:
        log.error("call '%s' not found.", call_id)
        sys.exit(1)

    if call.call_type != CallType.EVALUATE:
        log.error("call '%s' is a %s call, not an evaluation.", call_id, call.call_type.value)
        sys.exit(1)

    scope = await db.get_page(call.scope_page_id) if call.scope_page_id else None
    if scope:
        log.info("Evaluation for: %s", scope.headline[:80])
    log.info("Call: %s  Status: %s", call.id[:8], call.status.value)
    _print_evaluation(call)


async def cmd_summary(
    question_id: str,
    db: DB,
    max_depth: int = 4,
    summary_cutoff: int | None = None,
) -> str:
    question = await db.get_page(question_id)
    if not question:
        log.error("question '%s' not found. Run --list to see existing questions.", question_id)
        sys.exit(1)

    log.info("Generating summary for: %s", question.headline[:80])
    log.info("(This will use one LLM call but does not count against research budget)")

    summary_text = await generate_summary(
        question_id, db, max_depth=max_depth, summary_cutoff=summary_cutoff
    )
    path = save_summary(summary_text, question.headline)

    print(summary_text)
    log.info("Summary saved to: %s", path)
    return summary_text


async def cmd_self_improve(
    question_id: str,
    db: DB,
    *,
    instructions: str | None = None,
) -> None:
    question = await db.get_page(question_id)
    if not question:
        resolved = await db.resolve_page_id(question_id)
        if resolved:
            question = await db.get_page(resolved)
    if not question:
        log.error("question '%s' not found. Run --list to see existing questions.", question_id)
        sys.exit(1)

    if question.project_id and question.project_id != db.project_id:
        db.project_id = question.project_id

    log.info("Self-improvement analysis for: %s", question.headline[:80])
    log.info(
        "(Uses one or more LLM calls with read-only tools, does not count against research budget)"
    )
    if instructions and instructions.strip():
        print(f"Steering instructions: {instructions.strip()[:200]}\n")

    text = await run_self_improvement(question.id, db, instructions=instructions)
    if not text.strip():
        log.info("No analysis produced.")
        return
    path = save_self_improvement(text, question.headline)
    print(text)
    log.info("Self-improvement analysis saved to: %s", path)


async def cmd_report(
    question_id: str,
    db: DB,
    max_depth: int = 4,
) -> None:
    question = await db.get_page(question_id)
    if not question:
        log.error("question '%s' not found. Run --list to see existing questions.", question_id)
        sys.exit(1)

    log.info("Generating report for: %s", question.headline[:80])
    log.info("(This will use multiple LLM calls but does not count against research budget)")

    report_text = await generate_report(question_id, db, max_depth=max_depth)
    path = save_report(report_text, question.headline)

    print(report_text)
    log.info("Report saved to: %s", path)


async def cmd_draft_memos(
    scan_path: str,
    db: DB,
    *,
    indices: Sequence[int] | None = None,
    budget_per_memo: int = 30,
    refine_max_rounds: int = 10,
) -> None:
    path = Path(scan_path)
    if not path.exists():
        log.error("scan file '%s' not found.", scan_path)
        sys.exit(1)
    scan = load_scan_from_path(path)
    if not scan.root_question_id:
        log.error(
            "scan at '%s' has no root_question_id (it may pre-date that field). "
            "Re-run --scan-memos and try again.",
            scan_path,
        )
        sys.exit(1)

    root_question = await db.get_page(scan.root_question_id)
    if root_question is None:
        log.error(
            "root question %s from the scan is not present in this database. "
            "Are you in the right workspace?",
            scan.root_question_id[:8],
        )
        sys.exit(1)
    if root_question.project_id and root_question.project_id != db.project_id:
        db.project_id = root_question.project_id

    n_to_draft = len(scan.candidates) if indices is None else len(indices)

    budget = n_to_draft * budget_per_memo
    await db.init_budget(budget)

    log.info("Drafting %d memo(s) in parallel from scan: %s", n_to_draft, path.name)
    log.info("Investigation: %s", scan.root_question_headline[:80])
    log.info("Budget: %d (%d per memo x %d)", budget, budget_per_memo, n_to_draft)

    results = await draft_memos_from_scan(
        scan,
        db,
        indices=indices,
        refine_max_rounds=refine_max_rounds,
    )

    log.info("--- Draft summary ---")
    produced = 0
    for candidate, result, file_path in results:
        status = "ok" if result.artefact_id else "FAILED"
        log.info("[%s] %s", status, candidate.title[:70])
        if result.artefact_id:
            produced += 1
            log.info("    artefact: %s", result.artefact_id[:8])
            if file_path is not None:
                log.info("    file:     %s", file_path)
            log.info("    finalized: %s", result.finalized)
    log.info("Drafted %d/%d memos.", produced, n_to_draft)

    if produced > 0:
        log.info("Writing summary index...")
        summary_text = await generate_memo_summary(scan, results, db)
        summary_path = save_memo_summary(
            summary_text,
            scan.root_question_id,
            scan.root_question_headline,
        )
        log.info("Summary saved to: %s", summary_path)


async def cmd_scan_memos(
    question_id: str,
    db: DB,
    max_depth: int = 4,
) -> None:
    question = await db.get_page(question_id)
    if not question:
        resolved = await db.resolve_page_id(question_id)
        if resolved:
            question = await db.get_page(resolved)
    if not question:
        log.error("question '%s' not found. Run --list to see existing questions.", question_id)
        sys.exit(1)

    if question.project_id and question.project_id != db.project_id:
        db.project_id = question.project_id

    log.info("Scanning for memo candidates: %s", question.headline[:80])
    log.info("(One LLM call, does not count against research budget)")

    scan = await scan_for_memos(question.id, db, max_depth=max_depth)
    path = save_memo_scan(scan, question.headline)

    _reconfigure_stdout_utf8()
    print(render_scan_summary(scan))
    log.info("Memo scan saved to: %s", path)


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
        print(f"{q.id}  {counts['considerations']:>4}  {counts['judgements']:>4}  {truncated}")
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
    auto_summary: bool = False,
) -> str:
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
    log.info("New question: %s", question_id)
    log.info("Headline: %s", q.headline)
    log.info("Budget: %d research calls", budget)
    log.info("Trace: %s/traces/%s", frontend, db.run_id)
    _maybe_log_langfuse_session(db)

    if ingest_files:
        source_pages = []
        for filepath in ingest_files:
            page = await create_source_page(filepath, db)
            if page:
                source_pages.append(page)
        if source_pages:
            log.info("Ingesting %d source file(s)...", len(source_pages))
            await run_ingest_calls(source_pages, question_id, db)

    await Orchestrator(db).run(question_id)
    await _print_summary(db, suppress_hint=auto_summary)
    return question_id


def _batch_label(entry: dict) -> str:
    if "continue" in entry:
        return f"continue {entry['continue'][:8]}..."
    return entry["question"][:70]


async def _run_one_batch_entry(entry: dict, index: int, total: int, template_db: DB) -> str:
    """Run a single batch entry with its own run_id for budget isolation."""
    budget = entry.get("budget", 10)
    label = _batch_label(entry)

    db = await DB.create(
        run_id=str(uuid.uuid4()),
        client=template_db.client,
        project_id=template_db.project_id,
    )

    log.info("[%d/%d] Starting: %s (budget=%d)", index + 1, total, label, budget)

    if "continue" in entry:
        await cmd_continue(entry["continue"], budget, db)
    else:
        q = parse_question_input(entry["question"])
        await cmd_new(q, budget, db, ingest_files=entry.get("ingest"))

    log.info("[%d/%d] Done: %s", index + 1, total, label)
    return db.run_id


async def cmd_batch(batch_file: str, db: DB) -> list[str]:
    path = Path(batch_file)
    if not path.exists():
        log.error("file not found: %s", batch_file)
        sys.exit(1)

    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.error("reading batch file: %s", e)
        sys.exit(1)

    if not isinstance(entries, list) or not entries:
        log.error("batch file must contain a non-empty JSON array.")
        sys.exit(1)

    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            log.error("entry %d must be a JSON object.", i)
            sys.exit(1)
        if "question" not in entry and "continue" not in entry:
            log.error("entry %d must have a 'question' or 'continue' field.", i)
            sys.exit(1)

    total_budget = sum(e.get("budget", 10) for e in entries)
    new_count = sum(1 for e in entries if "question" in e)
    cont_count = sum(1 for e in entries if "continue" in e)
    parts = []
    if new_count:
        parts.append(f"{new_count} new")
    if cont_count:
        parts.append(f"{cont_count} continue")
    log.info("Batch: %s, total budget %d", " + ".join(parts), total_budget)
    log.info("Running concurrently...")

    tasks = [_run_one_batch_entry(entry, i, len(entries), db) for i, entry in enumerate(entries)]
    return list(await asyncio.gather(*tasks))


async def cmd_ab_eval(
    run_id_a: str,
    run_id_b: str,
    db: DB,
    agents_override: Sequence[EvalAgentSpec] | None = None,
) -> None:
    """Run A/B evaluation agents comparing two staged runs."""
    await run_ab_eval(run_id_a, run_id_b, db, agents_override=agents_override)


def resolve_eval_agents(
    names_csv: str | None,
) -> Sequence[EvalAgentSpec] | None:
    """Parse a comma-separated agent name string into a filtered agent list.

    Returns *None* (meaning "use all") when *names_csv* is falsy.
    """
    if not names_csv:
        return None
    by_name = {s.name: s for s in EVAL_AGENTS}
    requested = [n.strip() for n in names_csv.split(",")]
    unknown = [n for n in requested if n not in by_name]
    if unknown:
        valid = ", ".join(by_name)
        raise SystemExit(f"Unknown eval agent(s): {', '.join(unknown)}. Valid names: {valid}")
    return [by_name[n] for n in requested]


async def cmd_run_eval(
    run_id: str,
    db: DB,
    agents_override: Sequence[EvalAgentSpec] | None = None,
) -> None:
    """Evaluate a single staged run across all quality dimensions."""
    await run_run_eval(run_id, db, agents_override=agents_override)


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
        log.error("question '%s' not found. Run --list to see existing questions.", question_id)
        sys.exit(1)
    if question.page_type != PageType.QUESTION:
        log.error("page '%s' is a %s, not a question.", question_id, question.page_type.value)
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
    log.info("Continuing investigation of: %s", question.headline[:80])
    log.info("Question ID: %s", question_id)
    log.info(
        "Existing: %d considerations, %d judgements",
        counts["considerations"],
        counts["judgements"],
    )
    log.info("Budget: %d research calls", additional_budget)
    log.info("Trace: %s/traces/%s", frontend, db.run_id)
    _maybe_log_langfuse_session(db)

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
        existing_claim_ids = {p.id for p in await db.get_pages(page_type=PageType.CLAIM)}
        source_pages = []
        for filepath in ingest_files:
            page = await create_source_page(filepath, db)
            if page:
                source_pages.append(page)
                ingested_source_names.append(page.headline)
        if source_pages:
            log.info("Ingesting %d source file(s)...", len(source_pages))
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


async def _print_summary(db: DB, suppress_hint: bool = False) -> None:
    total, used = await db.get_budget()
    log.info("Budget used: %d/%d calls", used, total)
    if not suppress_hint:
        log.info("Run --list to see all questions.")


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
        nargs="?",
        const="__auto__",
        help=(
            "Generate an executive summary. Pass a QUESTION_ID to "
            "summarize an existing question, or combine with a new "
            "question to auto-summarize after investigation."
        ),
    )
    parser.add_argument(
        "--report",
        dest="report_id",
        metavar="QUESTION_ID",
        help="Generate a multi-section research report for a question",
    )
    parser.add_argument(
        "--scan-memos",
        dest="scan_memos_id",
        metavar="QUESTION_ID",
        help=(
            "Scan a completed investigation for the most important and "
            "surprising findings. Outputs a ranked list of memo candidates "
            "(title, content sketch, relevant page IDs, epistemic signals) "
            "as JSON, ready for a downstream memo drafter."
        ),
    )
    parser.add_argument(
        "--draft-memos",
        dest="draft_memos_path",
        metavar="SCAN_JSON_PATH",
        help=(
            "Draft memos from a saved scan JSON file produced by "
            "--scan-memos. Each candidate becomes one MemoOrchestrator run "
            "(generate_spec -> refine_spec -> artefact). Memos land both as "
            "ARTEFACT pages in the workspace and as markdown files under "
            "pages/memos/{question_short_id}/."
        ),
    )
    parser.add_argument(
        "--memo-indices",
        dest="memo_indices",
        metavar="N1,N2,...",
        default=None,
        help=(
            "Comma-separated 1-based candidate indices to draft (matches "
            "the ranking shown by --scan-memos). Default: all."
        ),
    )
    parser.add_argument(
        "--budget-per-memo",
        dest="budget_per_memo",
        type=int,
        default=30,
        help=(
            "Budget allocated per memo run (default: 30 — matches the "
            "generative orchestrator's typical cost: spec + refine + ~9 "
            "regenerate_and_critique cycles)."
        ),
    )
    parser.add_argument(
        "--self-improve",
        dest="self_improve_id",
        metavar="QUESTION_ID",
        nargs="?",
        const="__auto__",
        help=(
            "Analyse how a completed investigation went and suggest "
            "rumil code/prompt improvements. Read-only. Pass a "
            "QUESTION_ID to analyse an existing investigation, or "
            "combine with a new question to auto-analyse after "
            "investigation completes."
        ),
    )
    parser.add_argument(
        "--improvement-instructions",
        dest="improvement_instructions",
        metavar="TEXT",
        default=None,
        help=(
            "Optional free-text steer for --self-improve. The string is "
            "interpolated into the self-improvement prompt so the agent "
            "focuses its analysis on what you care about (e.g. "
            "'concentrate on prioritization quality and ignore prompt "
            "wording'). Has no effect without --self-improve."
        ),
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
        "--obsidian",
        dest="obsidian_dir",
        metavar="OUTPUT_DIR",
        help=(
            "Export pages as an Obsidian vault to OUTPUT_DIR. "
            "Pass a question ID as the positional arg to scope to that "
            "question's subtree. Combined with a new question text: "
            "auto-exports the question's subtree after investigation."
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
        "--user",
        dest="cli_user_id",
        default="",
        help=(
            "Supabase auth.users.id to stamp as the project owner on first creation. "
            "Overrides DEFAULT_CLI_USER_ID. Ignored for existing projects."
        ),
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
        "--view-variant",
        dest="view_variant",
        default=None,
        help="View variant (default: 'sectioned'). Options: 'sectioned' "
        "(importance-scored items), 'judgement' (flat NL judgement).",
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
        help="Shorthand for --db prod --executor prod (run as a k8s Job against prod Supabase).",
    )
    parser.add_argument(
        "--db",
        choices=["prod", "local"],
        default=None,
        help="Which Supabase to target. Default: local. Cannot be combined with --prod.",
    )
    parser.add_argument(
        "--executor",
        choices=["prod", "local"],
        default=None,
        help="Where to run. 'local' (default) runs in this process; 'prod' submits a "
        "Kubernetes Job via the rumil API. Cannot be combined with --prod.",
    )
    parser.add_argument(
        "--container-tag",
        dest="container_tag",
        default=None,
        metavar="TAG",
        help="Image tag override for --executor prod. The job runs against "
        "<registry>/rumil-api:<TAG> instead of the currently-deployed image. "
        "Used by scripts/remote_run.sh for experiment runs.",
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
        "--run-id",
        dest="run_id",
        metavar="UUID",
        default=None,
        help="Use this run_id instead of generating a new one. Set by the API "
        "when launching an orchestrator Job so the trace URL is known at submit time.",
    )
    parser.add_argument(
        "--env-file",
        dest="env_file",
        metavar="PATH",
        help="Load settings from this env file in addition to .env",
    )
    parser.add_argument(
        "--run-eval",
        dest="run_eval_id",
        metavar="RUN_ID",
        help="Evaluate a single staged run across all quality dimensions",
    )
    parser.add_argument(
        "--ab-eval",
        dest="ab_eval_ids",
        nargs=2,
        metavar=("RUN_ID_A", "RUN_ID_B"),
        help="Run A/B evaluation agents comparing two staged runs",
    )
    parser.add_argument(
        "--eval-agents",
        dest="eval_agent_names",
        metavar="NAMES",
        help="Comma-separated list of evaluation agent names to run "
        "(default: all). Available: grounding, coverage_and_relevance, "
        "depth_vs_breadth, research_redundancy, consistency, "
        "research_progress, general_quality",
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

    if args.prod_db and (args.db is not None or args.executor is not None):
        parser.error("--prod cannot be combined with explicit --db or --executor")
    explicit_remote = args.executor == "prod"
    db_choice = "prod" if args.prod_db else (args.db or "local")
    executor_choice = "prod" if args.prod_db else (args.executor or "local")
    if db_choice == "local" and executor_choice == "prod":
        parser.error(
            "--executor prod requires --db prod (the prod cluster cannot reach a local Supabase)"
        )
    # `--prod` is documented as a shorthand that targets prod for any command.
    # Non-orchestrator commands (--list, --summary ID, --report ID, etc.) only
    # have an in-process implementation, so silently keep them local; only an
    # explicit `--executor prod` is rejected for these modes (loud failure for
    # an explicit ask, soft fallback for the documented shorthand).
    if executor_choice == "prod" and not explicit_remote and _is_non_orchestrator_mode(args):
        executor_choice = "local"
    args.prod_db = db_choice == "prod"

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
        stream=sys.stdout,
    )
    logging.getLogger("rumil").setLevel(log_level)

    if args.env_file:
        _settings_var.set(Settings.from_env_files(".env", args.env_file))

    if args.available_moves is not None:
        get_settings().available_moves = args.available_moves
    if args.available_calls is not None:
        get_settings().available_calls = args.available_calls
    if args.view_variant is not None:
        get_settings().view_variant = args.view_variant
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

    if executor_choice == "prod":
        if (not args.question and not args.continue_id) or _is_non_orchestrator_mode(args):
            parser.error(
                "--executor prod is only supported for orchestrator runs (a question or "
                "--continue, with --budget). For other modes, omit --executor or use "
                "--executor local."
            )
        args.budget = _default_budget(args.budget)
        sys.exit(submit_remote_orchestrator_run(args))

    db = await DB.create(
        run_id=args.run_id or str(uuid.uuid4()), prod=args.prod_db, staged=args.staged
    )

    if args.run_id_file:
        Path(args.run_id_file).write_text(db.run_id, encoding="utf-8")

    if args.list_workspaces:
        await cmd_list_workspaces(db)
        return

    project = await db.get_or_create_project(
        args.workspace_name,
        owner_user_id=args.cli_user_id or get_settings().effective_cli_user_id or None,
    )
    db.project_id = project.id

    if args.stage_run_id:
        await db.stage_run(args.stage_run_id)
        log.info("Run %s has been staged.", args.stage_run_id)
        return

    if args.commit_run_id:
        await db.commit_staged_run(args.commit_run_id)
        log.info("Run %s has been committed.", args.commit_run_id)
        return

    eval_agents = resolve_eval_agents(args.eval_agent_names)

    if args.run_eval_id:
        await cmd_run_eval(args.run_eval_id, db, agents_override=eval_agents)
        return

    if args.ab_eval_ids:
        await cmd_ab_eval(
            args.ab_eval_ids[0],
            args.ab_eval_ids[1],
            db,
            agents_override=eval_agents,
        )
        return

    if args.obsidian_dir and not args.question:
        from rumil.obsidian_export import export_obsidian

        out = await export_obsidian(db, args.obsidian_dir)
        log.info("Exported to: %s", out)
        return

    if args.obsidian_dir and args.question:
        resolved = await db.resolve_page_id(args.question)
        if resolved:
            from rumil.obsidian_export import export_obsidian

            out = await export_obsidian(db, args.obsidian_dir, question_id=resolved)
            log.info("Exported to: %s", out)
            return
    run_ids: list[str] = []

    if args.list:
        await cmd_list(db, args.workspace_name)
        return
    elif args.evaluate_id:
        await cmd_evaluate(args.evaluate_id, db, eval_type=args.eval_type)
        return
    elif args.ground_call_id:
        await cmd_ground(args.ground_call_id, db, from_stage=args.from_stage)
        run_ids.append(db.run_id)
    elif args.feedback_call_id:
        await cmd_feedback_update(args.feedback_call_id, db, investigation_budget=args.budget)
        run_ids.append(db.run_id)
    elif args.feedback_file:
        await cmd_feedback_update_from_file(
            args.feedback_file[0],
            args.feedback_file[1],
            db,
            investigation_budget=args.budget,
        )
        run_ids.append(db.run_id)
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
        run_ids.append(db.run_id)
    elif args.chat_id:
        await run_chat(args.chat_id, db)
    elif args.add_question:
        q = parse_question_input(args.add_question)
        await cmd_add_question(q, args.parent_id, args.budget, db)
    elif args.summary_id and args.summary_id != "__auto__":
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
    elif args.scan_memos_id:
        await cmd_scan_memos(
            args.scan_memos_id,
            db,
            max_depth=args.max_depth,
        )
    elif args.draft_memos_path:
        memo_indices: Sequence[int] | None = None
        if args.memo_indices:
            try:
                memo_indices = [int(x) for x in args.memo_indices.split(",") if x.strip()]
            except ValueError:
                log.error("--memo-indices must be a comma-separated list of integers.")
                sys.exit(1)
        await cmd_draft_memos(
            args.draft_memos_path,
            db,
            indices=memo_indices,
            budget_per_memo=args.budget_per_memo,
        )
    elif args.self_improve_id and args.self_improve_id != "__auto__":
        await cmd_self_improve(
            args.self_improve_id,
            db,
            instructions=args.improvement_instructions,
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
        run_ids.append(db.run_id)
    elif args.batch_file:
        run_ids.extend(await cmd_batch(args.batch_file, db))
    elif args.ingest_files and not args.question:
        await cmd_ingest(args.ingest_files, args.for_question_id, args.budget, db)
    elif args.question:
        q = parse_question_input(args.question)
        do_summary = args.summary_id == "__auto__"
        do_self_improve = args.self_improve_id == "__auto__"
        question_id = await cmd_new(
            q,
            args.budget,
            db,
            ingest_files=args.ingest_files,
            name=args.run_name,
            auto_summary=do_summary,
        )
        summary_text = ""
        run_ids.append(db.run_id)
        if do_summary:
            summary_text = await cmd_summary(
                question_id,
                db,
                max_depth=args.max_depth,
                summary_cutoff=args.summarize_after_depth,
            )
        if args.obsidian_dir:
            from rumil.obsidian_export import export_obsidian

            out = await export_obsidian(
                db,
                args.obsidian_dir,
                question_id=question_id,
                summary_text=summary_text or None,
            )
            log.info("Obsidian vault exported to: %s", out)
        if do_self_improve:
            await cmd_self_improve(
                question_id,
                db,
                instructions=args.improvement_instructions,
            )
    else:
        parser.print_help()

    if len(run_ids) == 1:
        log.info("Run ID: %s", run_ids[0])
    elif run_ids:
        log.info("Run IDs: %s", ", ".join(run_ids))


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
