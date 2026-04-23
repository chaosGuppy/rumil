"""Bridge from the versus pairwise-judging harness to rumil's agent machinery.

Versus produces pairs of essay continuations with known ground truth (the
human remainder). This module lets us use rumil's agent + orchestrator
infrastructure as the judge on those pairs, so we can measure how well
rumil discriminates vs. single-shot OpenRouter judges.

Two judging paths exposed:

- ``judge_pair_ws_aware`` -- one agent call with single-arm workspace
  tools (search / load_page / explore_subgraph). The agent reads the
  pair from a Question page at its scope and optionally consults
  workspace material. Produces a trace URL and a 7-point preference
  label.

- ``judge_pair_orch`` -- full ``TwoPhaseOrchestrator`` run against the
  per-pair Question, followed by a cheap closer call that reads the
  resulting research subgraph and emits the same 7-point label.

Task bodies are caller-supplied. ``get_rumil_dimension_body(name)`` reads
an essay-adapted dimension prompt from ``prompts/versus-<name>.md``;
callers that want versus's own criterion prompts pass the prompt text
directly. Neither path writes to the ``ab_eval_reports`` table -- all
verdicts are returned as ``JudgeResult`` for the caller (usually versus
itself) to mirror wherever it likes.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rumil.context import format_page
from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, text_call
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    Page,
    PageDetail,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator
from rumil.run_eval.runner import wrap_as_mcp_tool
from rumil.sdk_agent import SdkAgentConfig, run_sdk_agent
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.tracer import CallTrace, reset_trace, set_trace
from rumil.workspace_exploration.explore import make_explore_subgraph_tool
from rumil.workspace_exploration.load_page import make_load_page_tool
from rumil.workspace_exploration.search import make_search_tool

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
_TOOL_SERVER_NAME = "versus-judge-tools"

PREFERENCE_LABELS: Sequence[str] = (
    "A strongly preferred",
    "A somewhat preferred",
    "A slightly preferred",
    "Approximately indifferent between A and B",
    "B slightly preferred",
    "B somewhat preferred",
    "B strongly preferred",
)

_LABEL_TO_VERDICT = {
    "A strongly preferred": "A",
    "A somewhat preferred": "A",
    "A slightly preferred": "A",
    "Approximately indifferent between A and B": "tie",
    "B slightly preferred": "B",
    "B somewhat preferred": "B",
    "B strongly preferred": "B",
}


@dataclass
class PairContext:
    """Inputs for one versus pairwise judgment.

    ``continuation_a_*`` / ``continuation_b_*`` are what the agent sees as
    "Continuation A" and "Continuation B" -- callers are responsible for
    putting them in display order (typically versus's deterministic
    ``order_pair``, so the same pair gets the same A/B assignment across
    all judges).

    ``source_a_id`` / ``source_b_id`` are the versus raw-pair source_ids
    in alphabetical order (versus's dedup-key convention). They are
    recorded as metadata on the Question page but NOT shown to the agent
    -- leaking the raw source_id (which can literally be ``"human"``)
    would break blind judging.
    """

    essay_id: str
    prefix_hash: str
    prefix_text: str
    continuation_a_id: str
    continuation_a_text: str
    continuation_b_id: str
    continuation_b_text: str
    source_a_id: str
    source_b_id: str
    task_name: str  # e.g. "general_quality", "grounding", "standalone_quality"


@dataclass
class JudgeResult:
    verdict: str | None
    preference_label: str | None
    reasoning_text: str
    trace_url: str
    call_id: str
    run_id: str
    question_id: str
    cost_usd: float


def extract_preference(text: str) -> str | None:
    """Return the 7-point label found in ``text``, or None if absent."""
    lower = text.lower()
    for label in PREFERENCE_LABELS:
        if label.lower() in lower:
            return label
    return None


def label_to_verdict(label: str | None) -> str | None:
    if label is None:
        return None
    return _LABEL_TO_VERDICT.get(label)


def get_rumil_dimension_body(name: str) -> str:
    """Load the essay-adapted rumil dimension prompt at ``prompts/versus-<name>.md``.

    ``name`` uses the same keys as :class:`rumil.run_eval.agents.EvalAgentSpec`
    (e.g. ``general_quality``, ``grounding``); underscores are converted to
    hyphens when resolving the file name.
    """
    path = _PROMPTS_DIR / f"versus-{name.replace('_', '-')}.md"
    if not path.is_file():
        raise ValueError(f"no essay-adapted dimension prompt for '{name}' (expected {path})")
    return path.read_text()


def build_system_prompt(task_body: str) -> str:
    """Compose the versus-judge shell with the task body slotted in."""
    shell = (_PROMPTS_DIR / "versus-judge-shell.md").read_text()
    return shell.replace("{task_body}", task_body)


def compute_prompt_hash(task_body: str) -> str:
    """Return a short hash of the composed system prompt.

    Covers both the shell and the task body, so any edit to either file
    invalidates judge_model dedup keys naturally -- mirroring versus's
    ``prefix_config_hash`` / ``sampling_hash`` discipline. 8 hex chars is
    enough to distinguish prompt versions without cluttering the key.
    """

    shell = (_PROMPTS_DIR / "versus-judge-shell.md").read_text()
    return hashlib.sha256((shell + task_body).encode()).hexdigest()[:8]


def _frontend_trace_url(run_id: str, call_id: str | None = None) -> str:
    base = get_settings().frontend_url.rstrip("/")
    anchor = f"#call-{call_id[:8]}" if call_id else ""
    return f"{base}/traces/{run_id}{anchor}"


def _versus_extra(pair: PairContext) -> dict:
    return {
        "source": "versus",
        "essay_id": pair.essay_id,
        "prefix_hash": pair.prefix_hash,
        "source_a_id": pair.source_a_id,
        "source_b_id": pair.source_b_id,
        "task_name": pair.task_name,
    }


def _format_pair_content(pair: PairContext) -> str:
    # Intentionally do NOT disclose continuation source_ids -- they can
    # literally be "human" and would defeat blind judging. Source ids are
    # preserved only in the Question's extra metadata.
    return (
        "This question was created by the versus pairwise essay-judging harness. "
        "Two continuations of the same essay opening are compared on one dimension. "
        "Workspace material may be consulted if it bears on the essay's subject.\n\n"
        f"## Dimension\n\n{pair.task_name}\n\n"
        f"## Essay opening\n\n{pair.prefix_text}\n\n"
        f"## Continuation A\n\n{pair.continuation_a_text}\n\n"
        f"## Continuation B\n\n{pair.continuation_b_text}\n"
    )


async def ensure_versus_question(db: DB, pair: PairContext) -> str:
    """Create a fresh Question page for this pair. Returns the page id.

    No reuse: each judgment invocation gets its own question. Dedup
    happens one layer up (at the versus judgments.jsonl level), so in
    practice we only create a question when a judgment is actually
    pending. Tagged ``extra.source="versus"`` for filterability.
    """
    headline = (
        f"Versus: {pair.task_name} -- {pair.source_a_id} vs {pair.source_b_id} ({pair.essay_id})"
    )
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=_format_pair_content(pair),
        headline=headline,
        project_id=db.project_id,
        provenance_model="versus-bridge",
        provenance_call_type=CallType.VERSUS_JUDGE.value,
        run_id=db.run_id,
        extra=_versus_extra(pair),
    )
    await db.save_page(page)
    return page.id


async def judge_pair_ws_aware(
    db: DB,
    pair: PairContext,
    *,
    task_body: str,
    broadcaster: Broadcaster | None = None,
) -> JudgeResult:
    """Run a single VERSUS_JUDGE agent call with single-arm workspace tools.

    The caller supplies ``task_body`` -- either the essay-adapted rumil
    dimension prompt (see :func:`get_rumil_dimension_body`) or a versus
    criterion prompt. It is slotted into ``prompts/versus-judge-shell.md``
    to produce the final system prompt.
    """
    question_id = await ensure_versus_question(db, pair)
    call = await db.create_call(
        call_type=CallType.VERSUS_JUDGE,
        scope_page_id=question_id,
    )
    trace = CallTrace(call.id, db, broadcaster=broadcaster)
    await db.update_call_status(call.id, CallStatus.RUNNING)

    explore_llm_tool = make_explore_subgraph_tool(db, trace, questions_only=False)
    load_page_llm_tool = make_load_page_tool(db, trace)
    search_llm_tool = make_search_tool(db, trace)
    mcp_tools = [
        wrap_as_mcp_tool(explore_llm_tool),
        wrap_as_mcp_tool(load_page_llm_tool),
        wrap_as_mcp_tool(search_llm_tool),
    ]

    system_prompt = build_system_prompt(task_body)
    user_prompt = (
        "Compare Continuation A and Continuation B on the dimension "
        f"**{pair.task_name}**.\n\n"
        f"The scope question (`{question_id}`) contains the essay "
        "prefix, continuation A, and continuation B. Read it with "
        "`load_page` if you don't already have the content in "
        "context. Use `search_workspace` and `explore_subgraph` if "
        "relevant workspace material exists on the essay's topic.\n\n"
        "End your response with one of the 7-point preference labels "
        "on its own line."
    )

    allowed = [
        f"mcp__{_TOOL_SERVER_NAME}__{t.name}"
        for t in (explore_llm_tool, load_page_llm_tool, search_llm_tool)
    ]

    config = SdkAgentConfig(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        server_name=_TOOL_SERVER_NAME,
        mcp_tools=mcp_tools,
        call=call,
        call_type=CallType.VERSUS_JUDGE,
        scope_page_id=question_id,
        db=db,
        trace=trace,
        broadcaster=broadcaster,
        allowed_tools=allowed,
        disallowed_tools=["Write", "Edit", "Glob"],
    )

    try:
        result = await run_sdk_agent(config)
        report_text = "\n\n".join(result.all_assistant_text)
        call.status = CallStatus.COMPLETE
        call.completed_at = datetime.now(UTC)
        call.result_summary = report_text[:500]
        if trace.total_cost_usd > 0:
            call.cost_usd = trace.total_cost_usd
        await db.save_call(call)
    except Exception:
        log.exception(
            "versus ws-aware judge failed (essay=%s, pair=%s/%s, task=%s)",
            pair.essay_id,
            pair.source_a_id,
            pair.source_b_id,
            pair.task_name,
        )
        await db.update_call_status(call.id, CallStatus.FAILED)
        raise

    label = extract_preference(report_text)
    return JudgeResult(
        verdict=label_to_verdict(label),
        preference_label=label,
        reasoning_text=report_text,
        trace_url=_frontend_trace_url(db.run_id, call.id),
        call_id=call.id,
        run_id=db.run_id,
        question_id=question_id,
        cost_usd=trace.total_cost_usd or 0.0,
    )


async def _run_orch_closer(
    db: DB,
    question_id: str,
    task_body: str,
    broadcaster: Broadcaster | None,
) -> tuple[str, Call]:
    """Small closing call: read the orchestrator's research on ``question_id``
    and emit a 7-point preference label.

    Wrapped in a VERSUS_JUDGE call so it gets its own trace.
    """
    question = await db.get_page(question_id)
    if question is None:
        raise RuntimeError(f"question {question_id} missing after orch run")
    rendered = await format_page(question, PageDetail.CONTENT, db=db)

    call = await db.create_call(
        call_type=CallType.VERSUS_JUDGE,
        scope_page_id=question_id,
    )
    trace = CallTrace(call.id, db, broadcaster=broadcaster)
    await db.update_call_status(call.id, CallStatus.RUNNING)

    system_prompt = build_system_prompt(task_body)
    user_prompt = (
        "A research run has just finished investigating the pair comparison "
        "captured in the scope question. The rendered question (including "
        "the essay prefix and both continuations) follows; your job is to "
        "read it, weigh what the research surfaced, and emit the 7-point "
        "preference label. Do not re-run the investigation -- this is the "
        "closing step.\n\n"
        f"{rendered}\n\n"
        "End your response with one of the 7-point preference labels on its "
        "own line."
    )

    token = set_trace(trace)
    try:
        report_text = await text_call(
            system_prompt=system_prompt,
            user_message=user_prompt,
            metadata=LLMExchangeMetadata(call_id=call.id, phase="versus_orch_closer"),
            db=db,
        )
        call.status = CallStatus.COMPLETE
        call.completed_at = datetime.now(UTC)
        call.result_summary = report_text[:500]
        if trace.total_cost_usd > 0:
            call.cost_usd = trace.total_cost_usd
        await db.save_call(call)
    except Exception:
        log.exception("versus orch closer failed (question=%s)", question_id)
        await db.update_call_status(call.id, CallStatus.FAILED)
        raise
    finally:
        reset_trace(token)

    return report_text, call


async def judge_pair_orch(
    db: DB,
    pair: PairContext,
    *,
    task_body: str,
    budget: int = 1,
    broadcaster: Broadcaster | None = None,
) -> JudgeResult:
    """Create a per-pair Question, run TwoPhaseOrchestrator against it, then
    fire a closing call to extract the 7-point label.

    ``budget`` is the orchestrator's research call budget; defaults to the
    minimum (1).
    """
    question_id = await ensure_versus_question(db, pair)
    await db.init_budget(budget)

    orch = TwoPhaseOrchestrator(db=db, broadcaster=broadcaster, budget_cap=budget)
    try:
        await orch.run(question_id)
    except Exception:
        log.exception(
            "versus orch failed (essay=%s, pair=%s/%s, task=%s)",
            pair.essay_id,
            pair.source_a_id,
            pair.source_b_id,
            pair.task_name,
        )
        raise

    report_text, closer_call = await _run_orch_closer(
        db,
        question_id,
        task_body=task_body,
        broadcaster=broadcaster,
    )

    label = extract_preference(report_text)
    run_calls = await db.get_calls_for_run(db.run_id)
    total_cost = sum((c.cost_usd or 0.0) for c in run_calls)
    return JudgeResult(
        verdict=label_to_verdict(label),
        preference_label=label,
        reasoning_text=report_text,
        trace_url=_frontend_trace_url(db.run_id, closer_call.id),
        call_id=closer_call.id,
        run_id=db.run_id,
        question_id=question_id,
        cost_usd=total_cost,
    )
