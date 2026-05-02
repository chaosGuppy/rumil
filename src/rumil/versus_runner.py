"""``run_versus``: universal entry point for versus runs.

Composes a :class:`Workflow` (the *how*) with a :class:`VersusTask`
(the *what*) into one end-to-end run. This is the layer
:func:`judge_pair_orch` and ``run_orch_completion`` shim through.

The four phases (research workflows, ``produces_artifact=False``):

1. ``task.create_question(db, inputs)`` — write the scope question.
2. ``workflow.setup(db, qid)`` then ``workflow.run(db, qid, broadcaster)``
   — fire the orchestrator (or other workflow shape).
3. ``task.render_for_closer(db, qid)`` then
   ``task.closer_prompts(rendered, inputs)`` — build the closer's prompts.
4. :func:`run_closer_agent` then ``task.extract_artifact(text)`` —
   single SDK-agent call, then parse the artifact.

For ``produces_artifact=True`` workflows (e.g. ``DraftAndEditWorkflow``
landing in #427) the runner skips phases 3-4: the workflow has already
written the final artifact text into ``question.content``, so we read
that verbatim and feed it straight into ``task.extract_artifact``. The
``call_id`` / ``trace_url`` on the returned result point at the most
recent call recorded under the run, and ``system_prompt`` /
``user_prompt`` are empty strings (no closer ran).

Cost accounting sums across every call recorded under the run id —
the workflow's research dispatches plus the closer call (when run).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

from rumil.database import DB
from rumil.model_config import ModelConfig
from rumil.settings import get_settings, override_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.versus_closer import run_closer_agent
from rumil.versus_workflow import Workflow

TInputs = TypeVar("TInputs")
TArtifact = TypeVar("TArtifact")

WorkflowStatus = Literal["complete", "incomplete", "failed"]


@dataclass
class VersusResult(Generic[TArtifact]):
    """End-to-end output of one ``run_versus`` invocation.

    ``status`` distinguishes "ran cleanly to completion" from
    "produced a partial artifact within budget" from "raised before
    finishing." Default ``"complete"``; workflows that have a partial-
    output failure mode (e.g. ``DraftAndEditWorkflow`` running out of
    budget mid-edit-round) override by setting ``last_status`` on the
    workflow instance — the runner reads it after ``workflow.run``
    and threads it onto the result.
    """

    artifact: TArtifact
    run_id: str
    call_id: str
    question_id: str
    trace_url: str
    cost_usd: float
    system_prompt: str
    user_prompt: str
    status: WorkflowStatus = "complete"


def _frontend_trace_url(run_id: str, call_id: str | None = None) -> str:
    base = get_settings().frontend_url.rstrip("/")
    anchor = f"#call-{call_id[:8]}" if call_id else ""
    return f"{base}/traces/{run_id}{anchor}"


async def run_versus(
    db: DB,
    *,
    workflow: Workflow,
    task,
    inputs,
    model: str,
    broadcaster: Broadcaster | None = None,
    model_config: ModelConfig | None = None,
) -> VersusResult:
    """Drive workflow + task end-to-end against ``db``.

    ``model`` scopes a :func:`override_settings` block around the entire
    workflow + closer call so the orchestrator's internal LLM calls all
    see the caller's chosen model.

    ``model_config`` (optional) pins thinking / effort / max_thinking_tokens
    for the closing call. The workflow's research calls go through
    ``call_anthropic_api`` too, but we don't currently override those —
    they pick up rumil's defaults for the model.
    """
    # rumil_model_override only accepts bare anthropic ids
    # (claude-{opus,sonnet,haiku}-...). versus's completion model
    # registry uses provider-namespaced ids ('anthropic/claude-...');
    # strip here so the orch path can pass either form. Keeps the
    # namespaced form in make_versus_config / source_id; only the
    # rumil-settings boundary needs the bare form.
    override_target = model.split("/", 1)[1] if "/" in model else model
    with override_settings(rumil_model_override=override_target):
        question_id = await task.create_question(db, inputs)
        await workflow.setup(db, question_id)
        await workflow.run(db, question_id, broadcaster)

        if workflow.produces_artifact:
            # Artifact-producing workflow path: the workflow has already
            # written the final text to question.content. Skip the closer
            # entirely and feed that text into extract_artifact. No
            # closer call ⇒ no system/user prompt; call_id falls back to
            # the most recent call on the run for trace anchoring.
            question = await db.get_page(question_id)
            if question is None:
                raise RuntimeError(
                    f"workflow {workflow.name} declared produces_artifact=True "
                    f"but question {question_id} is missing after run"
                )
            closer_text = question.content
            closer_call = None
            system_prompt = ""
            user_prompt = ""
        else:
            rendered = await task.render_for_closer(db, question_id)
            system_prompt, user_prompt = task.closer_prompts(rendered, inputs)
            closer_text, closer_call = await run_closer_agent(
                db,
                question_id=question_id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                call_type=task.call_type,
                max_turns=getattr(task, "sdk_max_turns", 5),
                disallowed_tools=getattr(task, "disallowed_tools", ("Write", "Edit", "Glob")),
                server_name=getattr(task, "tool_server_name", "versus-judge-tools"),
                broadcaster=broadcaster,
                model_config=model_config,
            )

    artifact = task.extract_artifact(closer_text)
    run_calls = await db.get_calls_for_run(db.run_id)
    total_cost = sum((c.cost_usd or 0.0) for c in run_calls)
    status: WorkflowStatus = getattr(workflow, "last_status", "complete")
    if closer_call is not None:
        anchor_call_id = closer_call.id
    else:
        # Pick the most recently created call on the run as the trace
        # anchor — typically the workflow's last dispatch. Falls back to
        # an empty string if the run somehow recorded no calls (e.g. a
        # workflow that wrote question.content without dispatching
        # anything; not currently possible but cheap to handle).
        latest = max(run_calls, key=lambda c: c.created_at, default=None)
        anchor_call_id = latest.id if latest is not None else ""
    return VersusResult(
        artifact=artifact,
        run_id=db.run_id,
        call_id=anchor_call_id,
        question_id=question_id,
        trace_url=_frontend_trace_url(db.run_id, anchor_call_id or None),
        cost_usd=total_cost,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        status=status,
    )
