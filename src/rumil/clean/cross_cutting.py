"""Cross-cutting feedback pipeline: identify and investigate subquestions
that span multiple top-level questions, then propagate results to all
affected question trees."""

import asyncio
import logging
from collections.abc import Sequence
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
from rumil.evaluate.explore import explore_page_impl
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
from rumil.tracing.trace_events import (
    CrossCuttingAnalysisCompleteEvent,
    UpdatePlanCreatedEvent,
)
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
_ANALYSIS_SERVER = "cross-cutting-analysis"
_PROPAGATION_SERVER = "cross-cutting-propagation"


class CrossCuttingSubquestion(BaseModel):
    question_id: str = Field(
        description="8-char short ID of the investigated subquestion"
    )
    headline: str = Field(description="Headline of the subquestion")
    parent_question_ids: list[str] = Field(
        description="8-char short IDs of the input questions this subquestion relates to"
    )
    judgement_summary: str = Field(
        description="Brief summary of the investigation's findings"
    )


class CrossCuttingAnalysis(BaseModel):
    subquestions: list[CrossCuttingSubquestion] = Field(
        description="Cross-cutting subquestions that were investigated"
    )


class _InvestigateCrossCuttingInput(BaseModel):
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
    parent_question_ids: list[str] = Field(
        description=(
            "List of 8-char short IDs of the parent questions this "
            "subquestion is relevant to. The subquestion is automatically "
            "linked as a child of ALL listed parents."
        ),
    )
    budget: int = Field(
        ge=MIN_TWOPHASE_BUDGET,
        description=f"Budget for the investigation (minimum {MIN_TWOPHASE_BUDGET})",
    )


def _make_investigate_cross_cutting_tool(
    call: Call, db: DB, broadcaster: Broadcaster | None, investigation_budget: int
):
    """MCP tool: commission investigation of a cross-cutting subquestion.

    Like feedback.py's investigate_question but links to MULTIPLE parents.
    """
    budget_remaining = investigation_budget
    budget_lock = asyncio.Lock()

    @tool(
        "investigate_cross_cutting",
        "Commission investigation of a cross-cutting subquestion. "
        "Spawns a full research cycle with its own budget "
        f"(minimum {MIN_TWOPHASE_BUDGET}). "
        "Can investigate an existing question (by question_id) or create "
        "a new question (by headline + content). The question is "
        "automatically linked as a child of ALL parent_question_ids. "
        "Each call's budget is deducted from a shared investigation pool.",
        _InvestigateCrossCuttingInput.model_json_schema(),
    )
    async def investigate_cross_cutting(args: dict) -> dict:
        nonlocal budget_remaining
        question_id = args.get("question_id", "")
        headline = args.get("headline", "")
        content = args.get("content", "")
        parent_question_ids: list[str] = args.get("parent_question_ids", [])
        budget = args["budget"]

        async with budget_lock:
            if budget > budget_remaining:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Rejected: requested budget {budget} exceeds "
                                f"remaining investigation budget of "
                                f"{budget_remaining}. "
                                f"Use a smaller budget or skip this investigation."
                            ),
                        }
                    ]
                }
            budget_remaining -= budget

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

        if not parent_question_ids:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Error: parent_question_ids must not be empty.",
                    }
                ]
            }

        resolved_parents: list[str] = []
        for pid in parent_question_ids:
            resolved = await db.resolve_page_id(pid)
            if not resolved:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Parent question '{pid}' not found.",
                        }
                    ]
                }
            resolved_parents.append(resolved)

        if headline:
            first_parent = await db.get_page(resolved_parents[0])
            ws = first_parent.workspace if first_parent else Workspace.RESEARCH
            proj_id = first_parent.project_id if first_parent else ""
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
            resolved_q = new_page.id
            display_headline = headline
            log.info(
                "Created new cross-cutting question %s: %s",
                resolved_q[:8],
                headline[:70],
            )
        else:
            resolved_q = await db.resolve_page_id(question_id)
            if not resolved_q:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Question '{question_id}' not found.",
                        }
                    ]
                }
            page = await db.get_page(resolved_q)
            display_headline = page.headline if page else resolved_q[:8]

        for parent_id in resolved_parents:
            await link_pages(
                from_id=parent_id,
                to_id=resolved_q,
                reasoning="Auto-linked by cross-cutting analysis",
                db=db,
                link_type=LinkType.CHILD_QUESTION,
            )

        orchestrator = ExperimentalOrchestrator(db, broadcaster, budget_cap=budget)
        orchestrator._parent_call_id = call.id
        child_call_id = await orchestrator.create_initial_call(
            resolved_q, parent_call_id=call.id
        )

        try:
            await orchestrator.run(resolved_q)
            judgement_text = ""
            judgements = await db.get_judgements_for_question(resolved_q)
            if judgements:
                latest = max(judgements, key=lambda j: j.created_at)
                judgement_text = (
                    f"\n\n## Judgement on [{resolved_q[:8]}]\n\n"
                    f"**{latest.headline}**\n\n{latest.content}"
                )
            parent_labels = ", ".join(p[:8] for p in resolved_parents)
            summary = (
                f"Investigation complete for [{resolved_q[:8]}] "
                f'"{display_headline}". '
                f"Linked to parents: [{parent_labels}]. "
                f"Child call: {child_call_id[:8]}. "
                f"Remaining investigation budget: {budget_remaining}."
                f"{judgement_text}"
            )
        except Exception:
            log.exception("investigate_cross_cutting failed for %s", resolved_q[:8])
            summary = (
                f"Investigation failed for [{resolved_q[:8]}] "
                f'"{display_headline}". '
                f"Child call: {child_call_id[:8]}. "
                f"Remaining investigation budget: {budget_remaining}."
            )

        return {"content": [{"type": "text", "text": summary}]}

    return investigate_cross_cutting


async def _build_multi_question_context(questions: Sequence[Page], db: DB) -> str:
    """Build context showing the local graph for each input question."""
    question_sections: list[str] = []
    for q in questions:
        graph_text = await explore_page_impl(q.id, db)
        question_sections.append(f"### `{q.id[:8]}` — {q.headline}\n\n{graph_text}")

    return "## Input questions\n\n" + "\n\n---\n\n".join(question_sections)


async def _analyze_and_investigate(
    questions: Sequence[Page],
    call: Call,
    db: DB,
    trace: CallTrace,
    broadcaster: Broadcaster | None = None,
) -> CrossCuttingAnalysis:
    """SDK agent stage: explore all input questions, identify cross-cutting
    themes, commission investigations, return analysis."""
    settings = get_settings()
    investigation_budget = settings.cross_cutting_investigation_budget
    advertised_budget = int(investigation_budget * 0.75)

    analysis_prompt = (_PROMPTS_DIR / "cross-cutting-analysis.md").read_text()
    system_prompt = (
        (_PROMPTS_DIR / "preamble.md").read_text()
        + "\n\n"
        + analysis_prompt.replace("{min_budget}", str(MIN_TWOPHASE_BUDGET)).replace(
            "{investigation_budget}", str(advertised_budget)
        )
    )

    context_text = await _build_multi_question_context(questions, db)

    explore_tool = make_explore_tool(db)
    investigate_tool = _make_investigate_cross_cutting_tool(
        call, db, broadcaster, advertised_budget
    )

    plan_tools = [explore_tool, investigate_tool]
    all_tool_fqnames = [f"mcp__{_ANALYSIS_SERVER}__{t.name}" for t in plan_tools]

    investigator_prompt = build_investigator_prompt("feedback")
    explore_fqname = f"mcp__{_ANALYSIS_SERVER}__explore_page"

    question_labels = ", ".join(
        f'`{q.id[:8]}` ("{q.headline[:60]}")' for q in questions
    )
    user_prompt = (
        f"Analyse these {len(questions)} input questions for cross-cutting "
        f"themes: {question_labels}\n\n"
        f"{context_text}"
    )

    config = SdkAgentConfig(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        server_name=_ANALYSIS_SERVER,
        mcp_tools=plan_tools,
        call=call,
        call_type=CallType.CROSS_CUTTING_UPDATE,
        scope_page_id=questions[0].id,
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
            "schema": CrossCuttingAnalysis.model_json_schema(),
        },
    )

    result = await run_sdk_agent(config)

    if result.structured_output is None:
        log.warning("Cross-cutting analysis agent returned no structured output")
        return CrossCuttingAnalysis(subquestions=[])

    return CrossCuttingAnalysis.model_validate(result.structured_output)


async def _plan_propagation(
    analysis: CrossCuttingAnalysis,
    questions: Sequence[Page],
    call: Call,
    db: DB,
    trace: CallTrace,
    broadcaster: Broadcaster | None = None,
) -> UpdatePlan:
    """SDK agent stage: plan propagation of investigation results across all
    affected question trees."""
    settings = get_settings()
    budget = settings.cross_cutting_update_budget

    propagation_prompt = (_PROMPTS_DIR / "cross-cutting-propagation.md").read_text()
    wave_prompt = (_PROMPTS_DIR / "update-waves.md").read_text()
    system_prompt = (
        (_PROMPTS_DIR / "preamble.md").read_text()
        + "\n\n"
        + propagation_prompt
        + "\n\n"
        + wave_prompt.replace("{budget}", str(budget))
    )

    explore_tool = make_explore_tool(db)
    prop_tools = [explore_tool]
    all_tool_fqnames = [f"mcp__{_PROPAGATION_SERVER}__{t.name}" for t in prop_tools]

    subq_lines: list[str] = []
    for sq in analysis.subquestions:
        parents = ", ".join(f"`{p}`" for p in sq.parent_question_ids)
        subq_lines.append(
            f"- `{sq.question_id}` — {sq.headline}\n"
            f"  Parents: {parents}\n"
            f"  Findings: {sq.judgement_summary}"
        )
    analysis_text = (
        "\n\n".join(subq_lines) if subq_lines else "(no subquestions investigated)"
    )

    context_text = await _build_multi_question_context(questions, db)

    user_prompt = (
        f"## Cross-cutting analysis results\n\n{analysis_text}\n\n{context_text}"
    )

    config = SdkAgentConfig(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        server_name=_PROPAGATION_SERVER,
        mcp_tools=prop_tools,
        call=call,
        call_type=CallType.CROSS_CUTTING_UPDATE,
        scope_page_id=questions[0].id,
        db=db,
        trace=trace,
        broadcaster=broadcaster,
        allowed_tools=all_tool_fqnames + ["Bash", "Read"],
        disallowed_tools=(),
        output_format={
            "type": "json_schema",
            "schema": UpdatePlan.model_json_schema(),
        },
    )

    result = await run_sdk_agent(config)

    if result.structured_output is None:
        log.warning("Cross-cutting propagation agent returned no structured output")
        return UpdatePlan(waves=[])

    plan = UpdatePlan.model_validate(normalize_plan(result.structured_output))
    log_plan(plan)
    return plan


async def run_cross_cutting_update(
    question_ids: Sequence[str],
    db: DB,
    *,
    broadcaster: Broadcaster | None = None,
    from_stage: int = 1,
    prior_checkpoints: dict | None = None,
) -> Call:
    """Run the cross-cutting feedback pipeline and return the Call record.

    *question_ids* is a list of resolved (full) question page IDs.
    *from_stage* (1-5) lets you resume from an intermediate stage.
    """
    questions: list[Page] = []
    for qid in question_ids:
        page = await db.get_page(qid)
        if page is None:
            raise ValueError(f'Question "{qid}" not found')
        questions.append(page)

    call = await db.create_call(
        call_type=CallType.CROSS_CUTTING_UPDATE,
        scope_page_id=questions[0].id,
    )
    trace = CallTrace(call.id, db, broadcaster=broadcaster)
    await db.update_call_status(call.id, CallStatus.RUNNING)
    await db.init_budget(get_settings().cross_cutting_investigation_budget)

    cp = prior_checkpoints or {}

    try:
        if from_stage <= 1:
            log.info(
                "Stage 1-2: analysing %d questions and commissioning investigations",
                len(questions),
            )
            analysis = await _analyze_and_investigate(
                questions=questions,
                call=call,
                db=db,
                trace=trace,
                broadcaster=broadcaster,
            )
            for sq in analysis.subquestions:
                parents = ", ".join(sq.parent_question_ids)
                log.info(
                    "  Cross-cutting subquestion [%s] '%s' (parents: [%s]): %s",
                    sq.question_id,
                    sq.headline[:80],
                    parents,
                    sq.judgement_summary[:120],
                )
            log.info(
                "Stage 1-2 complete: %d cross-cutting subquestions investigated",
                len(analysis.subquestions),
            )
            await trace.record(
                CrossCuttingAnalysisCompleteEvent(
                    subquestion_count=len(analysis.subquestions),
                    subquestions=[sq.model_dump() for sq in analysis.subquestions],
                )
            )
        else:
            analysis = CrossCuttingAnalysis(
                subquestions=[CrossCuttingSubquestion(**sq) for sq in cp["analysis"]]
            )
            log.info(
                "Stage 1-2: loaded analysis from prior run (%d subquestions)",
                len(analysis.subquestions),
            )

        save_checkpoint(
            call,
            "analysis",
            [sq.model_dump() for sq in analysis.subquestions],
        )
        await db.save_call(call)

        if from_stage <= 3:
            if analysis.subquestions:
                log.info("Stage 3: planning propagation across all question trees")
                plan = await _plan_propagation(
                    analysis=analysis,
                    questions=questions,
                    call=call,
                    db=db,
                    trace=trace,
                    broadcaster=broadcaster,
                )
            else:
                log.info("Stage 3: no subquestions to propagate")
                plan = UpdatePlan(waves=[])

            log.info(
                "Stage 3 complete: %d waves, %d operations",
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
                "Stage 3: loaded plan from prior run (%d waves, %d ops)",
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
            log.info("Stage 4: executing update plan")
            await execute_update_plan(plan, call, db, trace)
            log.info("Stage 4 complete")

        log.info("Stage 5: generating abstracts and embeddings")
        await generate_abstracts(call, db)
        log.info("Stage 5 complete")

        call.result_summary = (
            f"Cross-cutting update complete: "
            f"{len(analysis.subquestions)} cross-cutting subquestions investigated, "
            f"{total_ops} propagation operations executed "
            f"in {len(plan.waves)} waves."
        )
        call.status = CallStatus.COMPLETE
        await db.save_call(call)
    except Exception:
        log.exception("Cross-cutting update pipeline failed")
        await db.update_call_status(call.id, CallStatus.FAILED)
        raise

    return call
