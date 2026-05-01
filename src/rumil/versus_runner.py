"""``run_versus``: universal entry point for versus runs.

Composes a :class:`Workflow` (the *how*) with a :class:`VersusTask`
(the *what*) into one end-to-end run. This is the layer
:func:`judge_pair_orch` and (later) ``complete_essay`` shim through.

The four phases:

1. ``task.create_question(db, inputs)`` — write the scope question.
2. ``workflow.setup(db, qid)`` then ``workflow.run(db, qid, broadcaster)``
   — fire the orchestrator (or other workflow shape).
3. ``task.render_for_closer(db, qid)`` then
   ``task.closer_prompts(rendered, inputs)`` — build the closer's prompts.
4. :func:`run_closer_agent` then ``task.extract_artifact(text)`` —
   single SDK-agent call, then parse the artifact.

Cost accounting sums across every call recorded under the run id —
the workflow's research dispatches plus the closer call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

from rumil.database import DB
from rumil.model_config import ModelConfig
from rumil.models import CallType
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
    call_type: CallType = CallType.VERSUS_JUDGE,
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
    with override_settings(rumil_model_override=model):
        question_id = await task.create_question(db, inputs)
        await workflow.setup(db, question_id)
        await workflow.run(db, question_id, broadcaster)

        rendered = await task.render_for_closer(db, question_id)
        system_prompt, user_prompt = task.closer_prompts(rendered, inputs)

        closer_text, closer_call = await run_closer_agent(
            db,
            question_id=question_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            call_type=call_type,
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
    return VersusResult(
        artifact=artifact,
        run_id=db.run_id,
        call_id=closer_call.id,
        question_id=question_id,
        trace_url=_frontend_trace_url(db.run_id, closer_call.id),
        cost_usd=total_cost,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        status=status,
    )
