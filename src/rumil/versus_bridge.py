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
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import MagicMock

from rumil.context import format_page, render_view
from rumil.database import DB
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
from rumil.settings import get_settings, override_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.tracer import CallTrace
from rumil.workspace_exploration.explore import make_explore_subgraph_tool
from rumil.workspace_exploration.load_page import make_load_page_tool
from rumil.workspace_exploration.search import make_search_tool

log = logging.getLogger(__name__)

_TOOL_SERVER_NAME = "versus-judge-tools"

# Re-export the pure prompt-rendering helpers from the lightweight
# ``rumil.versus_prompts`` module so external callers (and existing
# imports) keep working unchanged.
from rumil.versus_prompts import (  # noqa: E402, F401
    PREFERENCE_LABELS,
    build_system_prompt,
    compute_prompt_hash,
    extract_preference,
    get_rumil_dimension_body,
    label_to_verdict,
)


def compute_tool_prompt_hash() -> str:
    """Short deterministic hash of the workspace-exploration tool prompts.

    Hashes the ``{tool_name: description_string}`` map for the three
    tools the rumil bridge exposes to ws-aware judges
    (``search_workspace``, ``load_page``, ``explore_subgraph``). Used as
    the ``:t<hash>`` suffix on ``rumil:ws:*`` and ``rumil:orch:*``
    judge_model strings so edits to those tool docstrings fork the
    dedup key.

    Scope decision (documented so it doesn't drift): this covers only
    the workspace-exploration family -- the tools directly passed to
    the SDK in ``judge_pair_ws_aware`` and ``_run_orch_closer``. The
    orchestrator's dispatched calls inside ``judge_pair_orch`` use a
    broader tool set (find_considerations, assess, scout-*, etc.)
    that isn't passed from this bridge; those are covered by
    ``code_fingerprint`` over the orchestrators / calls / prompts
    directories. Hashing workspace-exploration tool docstrings for
    both ws and orch keeps the key schemes parallel.

    Parameters match the bridge's actual call sites: load_page's
    default_detail is "content", explore_subgraph's questions_only is
    False (the ws bridge uses the full-graph variant). If either of
    those call-site parameters changes, the description text changes
    naturally and the hash forks without further intervention.
    """
    db_stub = MagicMock()
    trace_stub = MagicMock()
    search_tool = make_search_tool(db_stub, trace_stub)
    load_page_tool = make_load_page_tool(db_stub, trace_stub)
    explore_tool = make_explore_subgraph_tool(db_stub, trace_stub, questions_only=False)
    descriptions = {
        search_tool.name: search_tool.description,
        load_page_tool.name: load_page_tool.description,
        explore_tool.name: explore_tool.description,
    }
    blob = json.dumps(descriptions, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


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
    # The exact system + user prompt the judge was given. Stored on
    # the judgment row so audits can reconstruct what the judge saw
    # without chasing :p<hash> back through git history.
    system_prompt: str = ""
    user_prompt: str = ""


def _frontend_trace_url(run_id: str, call_id: str | None = None) -> str:
    base = get_settings().frontend_url.rstrip("/")
    anchor = f"#call-{call_id[:8]}" if call_id else ""
    return f"{base}/traces/{run_id}{anchor}"


def _versus_extra(pair: PairContext) -> dict:
    # IMPORTANT: every key in page.extra is rendered verbatim by
    # rumil.context.format_page() (as "key: value" lines inline with
    # the page body). So anything disclosing source identity leaks
    # to the agent. Keep only neutral tags.
    #
    # `essay_id` is also excluded — its `<source>__<slug>` namespacing
    # bakes the source into what looks like a neutral id, and it's the
    # one field that can route a capable agent toward the essay's
    # origin via workspace material. Operator-side correlation goes
    # through `runs.config.essay_id` (non-agent-visible) and the
    # judgment row's `essay_id` keyed by `question_id`.
    return {
        "source": "versus",
        "prefix_hash": pair.prefix_hash,
        "task_name": pair.task_name,
    }


def _build_headline(pair: PairContext) -> str:
    """Compose the Versus Question page headline.

    Intentionally source-free: `pair.essay_id` has the form
    `<source>__<slug>`, so using it here would leak the source into
    headline embedding / search / tool output. `prefix_hash[:8]`
    uniquely identifies the (essay, prefix_config) pair without
    disclosing where the essay came from. Operators can follow the
    prefix_hash back to the judgment row or `runs.config.essay_id`
    when they need the source.
    """
    return f"Versus judgment: {pair.task_name} [{pair.prefix_hash[:8]}]"


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


def _build_ws_user_prompt(pair: PairContext, question_id: str) -> str:
    """Compose the inline user message for the ws-aware agent call.

    Extracted from ``_judge_pair_ws_aware_inner`` so structural tests can
    verify it doesn't leak ``source_a_id`` / ``source_b_id`` (which can
    literally be ``"human"``). The agent reads the pair content via
    ``load_page`` on the scope question; this message is just the
    instruction shell.
    """
    return (
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


def _surface_hash_sentinel() -> dict[str, str]:
    """Build the sentinel dict from PairContext's actual fields, so
    adding/removing a field auto-updates the surface hash and hash
    coverage doesn't drift from the dataclass schema."""
    from dataclasses import fields

    return {f.name: f"_SENTINEL_{f.name.upper()}_" for f in fields(PairContext)}


def compute_pair_surface_hash() -> str:
    """Short deterministic hash of the Versus Question page surface.

    Used as the ``:q<hash>`` suffix on ``rumil:ws:*`` / ``rumil:orch:*``
    ``judge_model`` strings so structural edits to the agent-visible
    page surface auto-fork the dedup key without a manual version bump.

    Covers three surfaces together:

    - :func:`_build_headline` — the Question headline template.
    - :func:`_format_pair_content` — the Question body shape (section
      ordering, header text, etc.).
    - :func:`_versus_extra` — the set of keys stored on ``page.extra``
      (values are pair-dependent and live in the content body instead;
      only the key schema is hashed).

    Scope: ws/orch only. The blind path (single-turn LLM call, no DB)
    doesn't read the Question page, so a page-surface edit there
    wouldn't affect blind judgments — forking blind keys for a
    surface edit would force unnecessary re-judging. Inline user
    prompts, ``disallowed_tools``, and the orchestrator-internal
    tool set are covered by ``code_fingerprint`` over the bridge +
    orchestrator + calls files.
    """
    sentinel = PairContext(**_surface_hash_sentinel())
    blob = json.dumps(
        {
            "headline": _build_headline(sentinel),
            "content": _format_pair_content(sentinel),
            "extra_keys": sorted(_versus_extra(sentinel).keys()),
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


async def ensure_versus_question(db: DB, pair: PairContext) -> str:
    """Create a fresh Question page for this pair. Returns the page id.

    No reuse: each judgment invocation gets its own question. Dedup
    happens one layer up (at the versus_judgments level), so in practice
    we only create a question when a judgment is actually pending. Tagged ``extra.source="versus"`` for filterability; raw
    source ids stay in ``extra`` only and are NOT put in the headline
    or content (those render into the question's view / get loaded by
    the agent's tools, so any leak there defeats blind judging).
    """
    headline = _build_headline(pair)
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
    model: str,
    broadcaster: Broadcaster | None = None,
) -> JudgeResult:
    """Run a single VERSUS_JUDGE agent call with single-arm workspace tools.

    The caller supplies ``task_body`` -- either the essay-adapted rumil
    dimension prompt (see :func:`get_rumil_dimension_body`) or a versus
    criterion prompt. It is slotted into ``prompts/versus-judge-shell.md``
    to produce the final system prompt.

    ``model`` is the Anthropic model id passed through to the agent via
    a scoped :func:`override_settings` block so downstream rumil code
    (sdk_agent, text_call, any nested calls) reads the same value. No
    env-var gymnastics.
    """
    with override_settings(rumil_model_override=model):
        return await _judge_pair_ws_aware_inner(
            db, pair, task_body=task_body, broadcaster=broadcaster
        )


async def _judge_pair_ws_aware_inner(
    db: DB,
    pair: PairContext,
    *,
    task_body: str,
    broadcaster: Broadcaster | None,
) -> JudgeResult:
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

    system_prompt = build_system_prompt(task_body, with_tools=True)
    user_prompt = _build_ws_user_prompt(pair, question_id)

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
        # See _run_orch_closer for the rationale: the shared prompt shell
        # pins the verdict to the final turn, so label extraction should
        # only scan the final turn's text.
        report_text = "\n\n".join(result.last_assistant_text)
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
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        trace_url=_frontend_trace_url(db.run_id, call.id),
        call_id=call.id,
        run_id=db.run_id,
        question_id=question_id,
        cost_usd=trace.total_cost_usd or 0.0,
    )


async def _render_question_for_closer(db: DB, question_id: str) -> str:
    """Render a Question + the orchestrator's research into the closer prompt.

    ``format_page`` on a Question already surfaces considerations and
    judgements as linked items, but only at HEADLINE detail and without
    the View / view_items the orchestrator synthesizes. That made the
    closer effectively read preformed claim titles with no evidence and
    ignore the most distilled layer of research it had paid for.

    This helper extends the standard Question rendering along three
    axes: considerations and judgements render at CONTENT detail
    (claim body + link reasoning), and the active View page + all of
    its view_items are rendered below via :func:`render_view` at
    ``min_importance=2`` so every item the orch wrote is visible. Kept
    versus-specific to avoid changing how the non-versus orchestrator
    / call-site code paths render Question pages.
    """
    question = await db.get_page(question_id)
    if question is None:
        raise RuntimeError(f"question {question_id} missing after orch run")
    body = await format_page(
        question,
        PageDetail.CONTENT,
        linked_detail=PageDetail.CONTENT,
        db=db,
    )
    view = await db.get_view_for_question(question_id)
    if view is None:
        return body
    items = await db.get_view_items(view.id, min_importance=2)
    view_rendered = await render_view(view, items, min_importance=2)
    return f"{body}\n\n{view_rendered}"


_CLOSER_USER_PROMPT_TEMPLATE = (
    "A research run has just finished investigating the pair comparison "
    "captured in the scope question. The rendered question (including "
    "the essay prefix, both continuations, the considerations and "
    "judgements the orchestrator produced, and the distilled view "
    "items) follows; your job is to read it, weigh what the research "
    "surfaced, and emit the 7-point preference label. You have the "
    "workspace tools if further material bears on the essay's subject, "
    "but keep usage light — this is the closing step, not a fresh "
    "investigation.\n\n"
    "{rendered}\n\n"
    "End your response with one of the 7-point preference labels on its "
    "own line."
)
_CLOSER_SDK_MAX_TURNS = 5
_CLOSER_DISALLOWED_TOOLS = ("Write", "Edit", "Glob")
_CLOSER_RENDER_DETAIL = "CONTENT"
_CLOSER_RENDER_LINKED_DETAIL = "CONTENT"
_CLOSER_RENDER_MIN_IMPORTANCE = 2


def compute_orch_closer_hash() -> str:
    """Short deterministic hash of the orch closer's invariant config.

    Covers the parts of ``_run_orch_closer`` that the prompt-hash and
    tool-prompt-hash don't: the inline user-prompt template, the
    SDK agent's max_turns budget, the disallowed-tools set, and the
    rendering knobs the closer reads (page detail level, linked
    detail, view min_importance). Folded into the orch config dict
    as ``closer_hash`` so an edit here auto-forks ``config_hash``.

    orch-only — the blind and ws paths don't have a closer step.
    """
    blob = json.dumps(
        {
            "user_prompt_template": _CLOSER_USER_PROMPT_TEMPLATE,
            "sdk_agent_max_turns": _CLOSER_SDK_MAX_TURNS,
            "disallowed_tools": list(_CLOSER_DISALLOWED_TOOLS),
            "render_detail": _CLOSER_RENDER_DETAIL,
            "render_linked_detail": _CLOSER_RENDER_LINKED_DETAIL,
            "render_min_importance": _CLOSER_RENDER_MIN_IMPORTANCE,
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


async def _run_orch_closer(
    db: DB,
    question_id: str,
    task_body: str,
    broadcaster: Broadcaster | None,
    *,
    render_fn: Callable[[DB, str], Awaitable[str]] | None = None,
) -> tuple[str, Call, str, str]:
    """Small closing call: read the orchestrator's research on ``question_id``
    and emit a 7-point preference label.

    Wrapped in a VERSUS_JUDGE call so it gets its own trace. Runs as
    an SDK agent with the three workspace-exploration tools enabled
    and a tight ``max_turns`` budget — the system prompt promises
    those tools so we need to actually wire them. Budget is small
    because the closer's job is to synthesize what the orchestrator
    already produced, not to re-do the investigation.

    ``render_fn`` is an optional injection point for testing alternate
    closer contexts (e.g. expanded subtree rendering, view-only
    stripping of considerations). Defaults to
    :func:`_render_question_for_closer`. Production judge_pair_orch
    callers leave this alone; scripts that probe "does richer/poorer
    context change the verdict?" pass their own renderer and get
    everything else (SDK agent setup, tools, trace, call persistence)
    for free.
    """
    if render_fn is None:
        render_fn = _render_question_for_closer
    rendered = await render_fn(db, question_id)

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
    allowed = [
        f"mcp__{_TOOL_SERVER_NAME}__{t.name}"
        for t in (explore_llm_tool, load_page_llm_tool, search_llm_tool)
    ]

    system_prompt = build_system_prompt(task_body, with_tools=True)
    user_prompt = _CLOSER_USER_PROMPT_TEMPLATE.format(rendered=rendered)

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
        disallowed_tools=list(_CLOSER_DISALLOWED_TOOLS),
    )

    try:
        with override_settings(sdk_agent_max_turns=_CLOSER_SDK_MAX_TURNS):
            result = await run_sdk_agent(config)
        # Both the versus-judge-shell system prompt and the inline user
        # prompt instruct "End your response with ... on its own line", so
        # the verdict belongs in the FINAL turn. Scan only last_assistant_text
        # for the label — earlier turns may mention labels mid-thought
        # ("might be A somewhat preferred") that shouldn't count.
        report_text = "\n\n".join(result.last_assistant_text)
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

    return report_text, call, system_prompt, user_prompt


async def judge_pair_orch(
    db: DB,
    pair: PairContext,
    *,
    task_body: str,
    model: str,
    budget: int,
    broadcaster: Broadcaster | None = None,
) -> JudgeResult:
    """Create a per-pair Question, run TwoPhaseOrchestrator against it, then
    fire a closing call to extract the 7-point label.

    ``budget`` is the orchestrator's research call budget; caller must pass
    it explicitly -- a silent default hid the cost knob and made it too
    easy to run orch at budget=1 without realizing. ``model`` scopes a
    :func:`override_settings` block around the entire orchestrator run +
    closer call so the orchestrator's internal LLM calls all see the
    caller's chosen model.
    """
    with override_settings(rumil_model_override=model):
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

        report_text, closer_call, closer_system_prompt, closer_user_prompt = await _run_orch_closer(
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
        system_prompt=closer_system_prompt,
        user_prompt=closer_user_prompt,
        trace_url=_frontend_trace_url(db.run_id, closer_call.id),
        call_id=closer_call.id,
        run_id=db.run_id,
        question_id=question_id,
        cost_usd=total_cost,
    )
