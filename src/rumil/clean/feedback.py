"""General feedback pipeline: improve workspace based on feedback evaluation output."""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from claude_agent_sdk import AgentDefinition, tool

from rumil.clean.common import (
    UpdateOperation,
    UpdatePlan,
    execute_update_plan,
    generate_abstracts,
    log_plan,
    normalize_plan,
    save_checkpoint,
)
from rumil.constants import MIN_TWOPHASE_BUDGET
from rumil.database import DB
from rumil.evaluate.prompt import build_investigator_prompt
from rumil.explore_tool import make_explore_tool
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.moves.base import link_pages, write_page_file
from rumil.orchestrators.experimental import ExperimentalOrchestrator
from rumil.sdk_agent import SdkAgentConfig, run_sdk_agent
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import UpdatePlanCreatedEvent
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
_SERVER_NAME = "feedback-plan"


class _InvestigateQuestionInput(BaseModel):
    question_id: str = Field(
        default="",
        description=(
            "8-char short ID of an existing question to investigate. "
            "Mutually exclusive with headline."
        ),
    )
    headline: str = Field(
        default="",
        description=(
            "Headline for a NEW question to create and investigate. "
            "Mutually exclusive with question_id."
        ),
    )
    content: str = Field(
        default="",
        description="Content/description for a new question (used with headline).",
    )
    parent_question_id: str = Field(
        description=(
            "8-char short ID of the parent question. "
            "The investigated question is automatically linked as a child."
        ),
    )
    budget: int = Field(
        ge=MIN_TWOPHASE_BUDGET,
        description=f"Budget for the investigation (minimum {MIN_TWOPHASE_BUDGET})",
    )


@dataclass
class _PendingInvestigation:
    """A background investigation awaiting completion."""

    question_id: str
    display_headline: str
    child_call_id: str
    budget: int
    task: asyncio.Task[str]


def _make_investigation_tools(
    call: Call, db: DB, broadcaster: Broadcaster | None, investigation_budget: int
):
    """Create the investigate_question and collect_investigations MCP tools.

    Returns a tuple of (investigate_tool, collect_tool) to register on the
    MCP server.  ``investigate_question`` dispatches each investigation as a
    background ``asyncio.Task`` and returns immediately so that the Claude
    Agent SDK (which executes MCP tool calls serially) does not block on
    long-running orchestrator runs.  ``collect_investigations`` awaits all
    pending tasks and returns their results.

    *investigation_budget* is the total budget pool shared across all calls.
    """
    budget_remaining = investigation_budget
    budget_lock = asyncio.Lock()
    pending: dict[str, _PendingInvestigation] = {}

    async def _run_investigation(
        question_id: str,
        display_headline: str,
        budget: int,
    ) -> str:
        """Run orchestrator and return a summary string."""
        orchestrator = ExperimentalOrchestrator(db, broadcaster, budget_cap=budget)
        orchestrator._parent_call_id = call.id
        child_call_id = await orchestrator.create_initial_call(
            question_id, parent_call_id=call.id
        )
        try:
            await orchestrator.run(question_id)
            judgement_text = ""
            judgements = await db.get_judgements_for_question(question_id)
            if judgements:
                latest = max(judgements, key=lambda j: j.created_at)
                judgement_text = (
                    f"\n\n## Judgement on [{question_id[:8]}]\n\n"
                    f"**{latest.headline}**\n\n{latest.content}"
                )
            return (
                f"Investigation complete for [{question_id[:8]}] "
                f'"{display_headline}". Child call: {child_call_id[:8]}. '
                f"Remaining investigation budget: {budget_remaining}."
                f"{judgement_text}"
            )
        except Exception:
            log.exception("investigate_question failed for %s", question_id[:8])
            return (
                f"Investigation failed for [{question_id[:8]}] "
                f'"{display_headline}". Child call: {child_call_id[:8]}. '
                f"Remaining investigation budget: {budget_remaining}."
            )

    @tool(
        "investigate_question",
        "Commission deeper investigation of a subquestion. Spawns a full "
        f"research cycle with its own budget (minimum {MIN_TWOPHASE_BUDGET}). "
        "Can investigate an existing question (by question_id) or create a "
        "new question (by headline + content). Either way, the question is "
        "automatically linked as a child of parent_question_id. "
        "Each call's budget is deducted from a shared investigation budget "
        "pool — check the remaining budget in the tool response. "
        "This tool returns immediately — the investigation runs in the "
        "background. Call collect_investigations to wait for results.",
        _InvestigateQuestionInput.model_json_schema(),
    )
    async def investigate_question(args: dict) -> dict:
        nonlocal budget_remaining
        question_id = args.get("question_id", "")
        headline = args.get("headline", "")
        content = args.get("content", "")
        parent_question_id = args["parent_question_id"]
        budget = args["budget"]

        if not question_id and not headline:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Error: provide either question_id "
                            "(existing question) or headline (new question)."
                        ),
                    }
                ]
            }

        parent_resolved = await db.resolve_page_id(parent_question_id)
        if not parent_resolved:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (f"Parent question '{parent_question_id}' not found."),
                    }
                ]
            }

        if not headline:
            resolved = await db.resolve_page_id(question_id)
            if not resolved:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Question '{question_id}' not found.",
                        }
                    ]
                }

        async with budget_lock:
            if budget > budget_remaining:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Rejected: requested budget {budget} exceeds "
                                "remaining investigation budget of "
                                f"{budget_remaining}. "
                                "Use a smaller budget or skip this investigation."
                            ),
                        }
                    ]
                }
            budget_remaining -= budget

        if headline:
            parent_page = await db.get_page(parent_resolved)
            ws = parent_page.workspace if parent_page else Workspace.RESEARCH
            proj_id = parent_page.project_id if parent_page else ""
            new_page = Page(
                page_type=PageType.QUESTION,
                layer=PageLayer.SQUIDGY,
                workspace=ws,
                content=content or headline,
                headline=headline,
                provenance_model="claude-opus-4-6",
                provenance_call_type=call.call_type.value,
                provenance_call_id=call.id,
                project_id=proj_id,
            )
            await db.save_page(new_page)
            write_page_file(new_page)
            resolved = new_page.id
            display_headline = headline
            log.info(
                "Created new question %s: %s",
                resolved[:8],
                headline[:70],
            )
        else:
            resolved = await db.resolve_page_id(question_id)
            assert resolved  # validated before budget deduction
            page = await db.get_page(resolved)
            display_headline = page.headline if page else resolved[:8]

        await link_pages(
            from_id=parent_resolved,
            to_id=resolved,
            reasoning="Auto-linked by feedback update investigation",
            db=db,
            link_type=LinkType.CHILD_QUESTION,
        )

        task = asyncio.create_task(
            _run_investigation(resolved, display_headline, budget)
        )
        short_id = resolved[:8]
        pending[short_id] = _PendingInvestigation(
            question_id=resolved,
            display_headline=display_headline,
            child_call_id="",
            budget=budget,
            task=task,
        )
        log.info(
            "Dispatched background investigation for %s (%d pending)",
            short_id,
            len(pending),
        )

        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Investigation dispatched for [{short_id}] "
                        f'"{display_headline}" (budget {budget}). '
                        f"Remaining investigation budget: {budget_remaining}. "
                        f"{len(pending)} investigation(s) now running in the "
                        "background. Call collect_investigations when you are "
                        "done dispatching to wait for results."
                    ),
                }
            ]
        }

    @tool(
        "collect_investigations",
        "Wait for all pending background investigations to complete and "
        "return their results. Call this after dispatching all your "
        "investigate_question calls. Blocks until every investigation "
        "finishes.",
        {"type": "object", "properties": {}, "required": []},
    )
    async def collect_investigations(args: dict) -> dict:
        if not pending:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "No pending investigations to collect.",
                    }
                ]
            }

        log.info("Collecting %d pending investigations", len(pending))
        results: list[str] = []
        items = list(pending.items())
        tasks = [inv.task for _, inv in items]
        summaries = await asyncio.gather(*tasks, return_exceptions=True)
        for (short_id, inv), summary in zip(items, summaries):
            if isinstance(summary, BaseException):
                log.exception("Investigation %s raised", short_id, exc_info=summary)
                results.append(
                    f"Investigation FAILED for [{short_id}] "
                    f'"{inv.display_headline}": {summary}'
                )
            else:
                results.append(summary)
        pending.clear()

        return {"content": [{"type": "text", "text": "\n\n---\n\n".join(results)}]}

    return investigate_question, collect_investigations


async def _plan_and_edit(
    question: Page,
    evaluation_text: str,
    call: Call,
    db: DB,
    trace: CallTrace,
    broadcaster: Broadcaster | None = None,
) -> UpdatePlan:
    """SDK agent stage: explore graph, commission investigations, return propagation plan."""
    settings = get_settings()
    budget = settings.feedback_update_budget
    investigation_budget = settings.feedback_investigation_budget
    advertised_investigation_budget = int(investigation_budget * 0.75)

    plan_prompt = (_PROMPTS_DIR / "feedback-plan-updates.md").read_text()
    wave_prompt = (_PROMPTS_DIR / "update-waves.md").read_text()
    system_prompt = (
        (_PROMPTS_DIR / "preamble.md").read_text()
        + "\n\n"
        + plan_prompt.replace("{min_budget}", str(MIN_TWOPHASE_BUDGET))
        .replace("{edit_budget}", str(budget))
        .replace("{investigation_budget}", str(advertised_investigation_budget))
        + "\n\n"
        + wave_prompt.replace("{budget}", str(budget))
    )

    explore_tool = make_explore_tool(db)
    investigate_tool, collect_tool = _make_investigation_tools(
        call, db, broadcaster, advertised_investigation_budget
    )

    plan_tools = [explore_tool, investigate_tool, collect_tool]
    all_tool_fqnames = [f"mcp__{_SERVER_NAME}__{t.name}" for t in plan_tools]

    investigator_prompt = build_investigator_prompt("feedback")
    explore_fqname = f"mcp__{_SERVER_NAME}__explore_page"

    user_prompt = (
        f"Feedback evaluation for question `{question.id[:8]}` "
        f'("{question.headline}"):\n\n'
        f"{evaluation_text}"
    )

    config = SdkAgentConfig(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        server_name=_SERVER_NAME,
        mcp_tools=plan_tools,
        call=call,
        call_type=CallType.FEEDBACK_UPDATE,
        scope_page_id=question.id,
        db=db,
        trace=trace,
        broadcaster=broadcaster,
        agents={
            "investigator": AgentDefinition(
                description=(
                    "Explores the research workspace graph to gather "
                    "information. Read-only — does not make edits."
                ),
                prompt=investigator_prompt,
                tools=[explore_fqname, "Read", "Grep", "Bash"],
            ),
        },
        allowed_tools=all_tool_fqnames + ["Agent", "Bash", "Read"],
        disallowed_tools=(),
        output_format={
            "type": "json_schema",
            "schema": UpdatePlan.model_json_schema(),
        },
    )

    result = await run_sdk_agent(config)

    if result.structured_output is None:
        log.warning("Feedback planning agent returned no structured output")
        return UpdatePlan(waves=[])

    plan = UpdatePlan.model_validate(normalize_plan(result.structured_output))
    log_plan(plan)
    return plan


async def run_feedback_update(
    question_id: str,
    evaluation_text: str,
    db: DB,
    *,
    broadcaster: Broadcaster | None = None,
    from_stage: int = 1,
    prior_checkpoints: dict | None = None,
) -> Call:
    """Run the feedback update pipeline and return the Call record.

    *from_stage* (1-3) lets you resume from an intermediate stage.
    When resuming, *prior_checkpoints* supplies the outputs of earlier
    stages (loaded from a previous call's ``call_params["checkpoints"]``).
    """
    question = await db.get_page(question_id)
    if question is None:
        raise ValueError(f'Question "{question_id}" not found')

    call = await db.create_call(
        call_type=CallType.FEEDBACK_UPDATE,
        scope_page_id=question_id,
    )
    trace = CallTrace(call.id, db, broadcaster=broadcaster)
    await db.update_call_status(call.id, CallStatus.RUNNING)
    await db.init_budget(get_settings().feedback_investigation_budget)

    cp = prior_checkpoints or {}

    try:
        if from_stage <= 1:
            log.info("Stage 1: planning and commissioning investigations")
            plan = await _plan_and_edit(
                question=question,
                evaluation_text=evaluation_text,
                call=call,
                db=db,
                trace=trace,
                broadcaster=broadcaster,
            )
            log.info(
                "Stage 1 complete: %d waves, %d operations",
                len(plan.waves),
                sum(len(w) for w in plan.waves),
            )
            await trace.record(
                UpdatePlanCreatedEvent(
                    wave_count=len(plan.waves),
                    operation_count=sum(len(w) for w in plan.waves),
                    waves=[[op.model_dump() for op in wave] for wave in plan.waves],
                )
            )
        else:
            plan = UpdatePlan(
                waves=[
                    [UpdateOperation(**op) for op in wave] for wave in cp["update_plan"]
                ]
            )
            log.info(
                "Stage 1: loaded plan from prior run (%d waves, %d ops)",
                len(plan.waves),
                sum(len(w) for w in plan.waves),
            )

        save_checkpoint(
            call,
            "update_plan",
            [[op.model_dump() for op in wave] for wave in plan.waves],
        )
        await db.save_call(call)

        total_ops = sum(len(w) for w in plan.waves)
        if total_ops == 0:
            log.info("No propagation operations planned")
        else:
            log.info("Stage 2: executing update plan")
            await execute_update_plan(plan, call, db, trace)
            log.info("Stage 2 complete")

        log.info("Stage 3: generating abstracts and embeddings")
        await generate_abstracts(call, db)
        log.info("Stage 3 complete")

        call.result_summary = (
            "Feedback update complete: "
            f"{total_ops} propagation operations executed "
            f"in {len(plan.waves)} waves."
        )
        call.status = CallStatus.COMPLETE
        await db.save_call(call)
    except Exception:
        log.exception("Feedback update pipeline failed")
        await db.update_call_status(call.id, CallStatus.FAILED)
        raise

    return call
