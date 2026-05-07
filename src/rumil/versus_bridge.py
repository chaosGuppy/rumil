"""Bridge from the versus pairwise-judging harness to rumil's machinery.

After #424, this module is a thin compatibility layer. The substance
moved into:

- :mod:`versus.tasks.judge_pair` — ``PairContext``, ``JudgePairTask``,
  the page-surface helpers, and the hash invariants
  (``compute_pair_surface_hash``, ``compute_tool_prompt_hash``,
  ``compute_closer_hash``).
- :mod:`rumil.versus_runner` — the ``run_versus`` entry point that
  drives a Workflow + Task pair end-to-end.
- :mod:`rumil.versus_closer` — the generic closer-agent loop.

What's left here is back-compat wiring: :func:`judge_pair_orch` is a
shim over ``run_versus``, plus re-exports of names that out-of-tree
callers still import from this module.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from versus.tasks.judge_pair import (  # noqa: F401
    JudgeArtifact,
    JudgePairTask,
    PairContext,
    _build_headline,
    _format_pair_content,
    _surface_hash_sentinel,
    _versus_extra,
    compute_closer_hash,
    compute_pair_surface_hash,
    compute_tool_prompt_hash,
)

from rumil.database import DB
from rumil.model_config import ModelConfig
from rumil.models import Call, CallType
from rumil.orchestrators.reflective_judge import ReflectiveJudgeWorkflow
from rumil.tracing.broadcast import Broadcaster
from rumil.versus_closer import run_closer_agent

# Re-export the pure prompt-rendering helpers from the lightweight
# ``rumil.versus_prompts`` module so external callers (and existing
# imports) keep working unchanged.
from rumil.versus_prompts import (
    PREFERENCE_LABELS,
    build_system_prompt,
    compute_prompt_hash,
    extract_preference,
    get_rumil_dimension_body,
    label_to_verdict,
)
from rumil.versus_runner import run_versus
from rumil.versus_workflow import TwoPhaseWorkflow

# Legacy alias — older callers (versus.rumil_judge, versus.mainline)
# import this name; #424 renamed the underlying helper to
# ``compute_closer_hash``. Keep the alias until those callers migrate.
compute_orch_closer_hash = compute_closer_hash

log = logging.getLogger(__name__)


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


async def ensure_versus_question(db: DB, pair: PairContext) -> str:
    """Back-compat: build the per-pair Question via :class:`JudgePairTask`.

    The standalone helper used to live here; #424 moved it onto
    :meth:`JudgePairTask.create_question`. Kept as a free function so
    test_versus_blind_judge / test_versus_bridge keep importing the
    same name.
    """
    # The dimension body isn't needed to build the question — only the
    # content layout is, and that lives in the task's create_question.
    # Use a placeholder body; it's not folded into anything written here.
    task = JudgePairTask(dimension=pair.task_name, dimension_body="")
    return await task.create_question(db, pair)


async def _render_question_for_closer(db: DB, question_id: str) -> str:
    """Back-compat: delegate to :meth:`JudgePairTask.render_for_closer`.

    Used by ``versus/scripts/rerun_orch_closer.py`` for ablation runs.
    Kept here so that script (and any outside-of-tree caller) doesn't
    have to migrate in lockstep with #424.
    """
    task = JudgePairTask(dimension="", dimension_body="")
    return await task.render_for_closer(db, question_id)


async def _run_orch_closer(
    db: DB,
    question_id: str,
    task_body: str,
    broadcaster: Broadcaster | None,
    *,
    render_fn: Callable[[DB, str], Awaitable[str]] | None = None,
    model_config: ModelConfig | None = None,
) -> tuple[str, Call, str, str]:
    """Back-compat: run the closer agent for a pre-existing orch run.

    Used only by ``versus/scripts/rerun_orch_closer.py`` for ablation
    runs that re-fire the closer against an existing orch run with a
    custom render. Production paths go through :func:`run_versus`
    which doesn't expose this seam.

    ``render_fn`` lets the script swap in alternative rendering
    (expanded subtree, view-only) while keeping all the SDK + tracing
    plumbing stock. Returns ``(text, call, system_prompt, user_prompt)``
    so the script can print/parse the result.
    """
    if render_fn is None:
        render_fn = _render_question_for_closer
    rendered = await render_fn(db, question_id)
    task = JudgePairTask(dimension="versus_dimension", dimension_body=task_body)
    system_prompt, user_prompt = task.closer_prompts(rendered, None)  # type: ignore[arg-type]
    text, call = await run_closer_agent(
        db,
        question_id=question_id,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        call_type=CallType.VERSUS_JUDGE,
        max_turns=task.sdk_max_turns,
        disallowed_tools=task.disallowed_tools,
        server_name=task.tool_server_name,
        broadcaster=broadcaster,
        model_config=model_config,
    )
    return text, call, system_prompt, user_prompt


async def judge_pair_orch(
    db: DB,
    pair: PairContext,
    *,
    task_body: str,
    model: str,
    budget: int,
    broadcaster: Broadcaster | None = None,
    model_config: ModelConfig | None = None,
) -> JudgeResult:
    """Back-compat shim — delegates to :func:`run_versus` with TwoPhase + JudgePair.

    ``budget`` is the orchestrator's research call budget; caller must
    pass it explicitly. ``model`` scopes a settings override around the
    entire run so the orchestrator's internal LLM calls all see the
    caller's chosen model. ``model_config`` (optional) pins
    thinking / effort / max_thinking_tokens for the closing call.

    Returned :class:`JudgeResult` mirrors the pre-#424 shape: verdict
    + preference label + closer text + trace metadata + recorded
    system/user prompts.
    """
    workflow = TwoPhaseWorkflow(budget=budget)
    task = JudgePairTask(dimension=pair.task_name, dimension_body=task_body)
    try:
        result = await run_versus(
            db,
            workflow=workflow,
            task=task,
            inputs=pair,
            model=model,
            broadcaster=broadcaster,
            model_config=model_config,
        )
    except Exception:
        log.exception(
            "versus orch failed (essay=%s, pair=%s/%s, task=%s)",
            pair.essay_id,
            pair.source_a_id,
            pair.source_b_id,
            pair.task_name,
        )
        raise
    return JudgeResult(
        verdict=result.artifact.verdict,
        preference_label=result.artifact.preference_label,
        reasoning_text=result.artifact.reasoning_text,
        trace_url=result.trace_url,
        call_id=result.call_id,
        run_id=result.run_id,
        question_id=result.question_id,
        cost_usd=result.cost_usd,
        system_prompt=result.system_prompt,
        user_prompt=result.user_prompt,
    )


async def judge_pair_reflective(
    db: DB,
    pair: PairContext,
    *,
    task_body: str,
    model: str,
    broadcaster: Broadcaster | None = None,
    model_config: ModelConfig | None = None,
    reader_model: str | None = None,
    reflector_model: str | None = None,
    verdict_model: str | None = None,
    read_prompt_path: str | Path | None = None,
    reflect_prompt_path: str | Path | None = None,
    verdict_prompt_path: str | Path | None = None,
) -> JudgeResult:
    """Run ReflectiveJudgeWorkflow on a pair — read → reflect → verdict.

    Mirrors :func:`judge_pair_orch`'s signature where it overlaps. No
    ``budget`` parameter — the workflow has a fixed 3 LLM calls. The
    per-role ``*_model`` overrides and ``*_prompt_path`` overrides are
    the iterate skill's primary levers; default ``None`` inherits from
    the bridge-set ``rumil_model_override`` and the workflow's built-in
    prompts respectively.

    ``task_body`` is the dimension rubric (e.g. the contents of
    ``versus-general-quality.md``) — the same surface fed to the orch
    and blind paths, so verdicts across variants are apples-to-apples
    for the same rubric.

    Returned :class:`JudgeResult` mirrors :func:`judge_pair_orch`.
    """
    workflow = ReflectiveJudgeWorkflow(
        dimension_body=task_body,
        reader_model=reader_model,
        reflector_model=reflector_model,
        verdict_model=verdict_model,
        read_prompt_path=read_prompt_path,
        reflect_prompt_path=reflect_prompt_path,
        verdict_prompt_path=verdict_prompt_path,
    )
    task = JudgePairTask(dimension=pair.task_name, dimension_body=task_body)
    try:
        result = await run_versus(
            db,
            workflow=workflow,
            task=task,
            inputs=pair,
            model=model,
            broadcaster=broadcaster,
            model_config=model_config,
        )
    except Exception:
        log.exception(
            "versus reflective_judge failed (essay=%s, pair=%s/%s, task=%s)",
            pair.essay_id,
            pair.source_a_id,
            pair.source_b_id,
            pair.task_name,
        )
        raise
    return JudgeResult(
        verdict=result.artifact.verdict,
        preference_label=result.artifact.preference_label,
        reasoning_text=result.artifact.reasoning_text,
        trace_url=result.trace_url,
        call_id=result.call_id,
        run_id=result.run_id,
        question_id=result.question_id,
        cost_usd=result.cost_usd,
        system_prompt=result.system_prompt,
        user_prompt=result.user_prompt,
    )


__all__ = (
    "PREFERENCE_LABELS",
    "JudgeArtifact",
    "JudgePairTask",
    "JudgeResult",
    "PairContext",
    "build_system_prompt",
    "compute_closer_hash",
    "compute_orch_closer_hash",
    "compute_pair_surface_hash",
    "compute_prompt_hash",
    "compute_tool_prompt_hash",
    "ensure_versus_question",
    "extract_preference",
    "get_rumil_dimension_body",
    "judge_pair_orch",
    "judge_pair_reflective",
    "label_to_verdict",
)
