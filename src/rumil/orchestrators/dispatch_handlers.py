"""
Registry-based dispatch handling for BaseOrchestrator._execute_dispatch.

Each payload type in DISPATCH_HANDLERS maps to an async handler that performs
the actual dispatch: invoking the right call-side helper or _run_simple_call_dispatch
with the right CallType / CallRunner class. Adding a new dispatchable call type
means adding an entry to DISPATCH_HANDLERS; no changes to BaseOrchestrator are
required.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rumil.calls.scout_analogies import ScoutAnalogiesCall
from rumil.calls.scout_c_cruxes import ScoutCCruxesCall
from rumil.calls.scout_c_how_false import ScoutCHowFalseCall
from rumil.calls.scout_c_how_true import ScoutCHowTrueCall
from rumil.calls.scout_c_relevant_evidence import ScoutCRelevantEvidenceCall
from rumil.calls.scout_c_robustify import ScoutCRobustifyCall
from rumil.calls.scout_c_strengthen import ScoutCStrengthenCall
from rumil.calls.scout_c_stress_test_cases import ScoutCStressTestCasesCall
from rumil.calls.scout_deep_questions import ScoutDeepQuestionsCall
from rumil.calls.scout_estimates import ScoutEstimatesCall
from rumil.calls.scout_factchecks import ScoutFactchecksCall
from rumil.calls.scout_hypotheses import ScoutHypothesesCall
from rumil.calls.scout_paradigm_cases import ScoutParadigmCasesCall
from rumil.calls.scout_subquestions import ScoutSubquestionsCall
from rumil.calls.scout_web_questions import ScoutWebQuestionsCall
from rumil.calls.stages import CallRunner
from rumil.models import (
    AssessDispatchPayload,
    BaseDispatchPayload,
    CallType,
    CreateViewDispatchPayload,
    MultiRoundFields,
    ScoutAnalogiesDispatchPayload,
    ScoutCCruxesDispatchPayload,
    ScoutCHowFalseDispatchPayload,
    ScoutCHowTrueDispatchPayload,
    ScoutCRelevantEvidenceDispatchPayload,
    ScoutCRobustifyDispatchPayload,
    ScoutCStrengthenDispatchPayload,
    ScoutCStressTestCasesDispatchPayload,
    ScoutDeepQuestionsDispatchPayload,
    ScoutDispatchPayload,
    ScoutEstimatesDispatchPayload,
    ScoutFactchecksDispatchPayload,
    ScoutHypothesesDispatchPayload,
    ScoutParadigmCasesDispatchPayload,
    ScoutSubquestionsDispatchPayload,
    ScoutWebQuestionsDispatchPayload,
    WebResearchDispatchPayload,
)
from rumil.orchestrators.common import (
    assess_question,
    find_considerations_until_done,
    web_research_question,
)
from rumil.views import get_active_view

if TYPE_CHECKING:
    from rumil.orchestrators.base import BaseOrchestrator


log = logging.getLogger(__name__)


@dataclass
class DispatchContext:
    """Bundle of per-dispatch state passed to each handler.

    Carries the orchestrator itself (so handlers can call
    _run_simple_call_dispatch, access self.db, etc.), the resolved
    target question ID, and the infrastructure fields that need to
    flow into the underlying call-side helper.
    """

    orchestrator: "BaseOrchestrator"
    resolved_question_id: str
    parent_call_id: str | None
    force: bool
    call_id: str | None
    sequence_id: str | None
    sequence_position: int | None
    d_label: str

    @property
    def pool_question_id(self) -> str | None:
        return self.orchestrator.pool_question_id


DispatchHandler = Callable[[DispatchContext, BaseDispatchPayload], Awaitable[str | None]]


async def _handle_find_considerations(
    ctx: DispatchContext, payload: BaseDispatchPayload
) -> str | None:
    assert isinstance(payload, ScoutDispatchPayload)
    log.info(
        "Dispatch: find_considerations on %s (fruit_threshold=%d, max_rounds=%d) — %s",
        ctx.d_label,
        payload.fruit_threshold,
        payload.max_rounds,
        payload.reason,
    )
    _, child_ids = await find_considerations_until_done(
        ctx.resolved_question_id,
        ctx.orchestrator.db,
        max_rounds=payload.max_rounds,
        fruit_threshold=payload.fruit_threshold,
        parent_call_id=ctx.parent_call_id,
        context_page_ids=payload.context_page_ids,
        broadcaster=ctx.orchestrator.broadcaster,
        force=ctx.force,
        call_id=ctx.call_id,
        sequence_id=ctx.sequence_id,
        sequence_position=ctx.sequence_position,
        pool_question_id=ctx.pool_question_id,
    )
    return child_ids[0] if child_ids else None


async def _handle_assess(ctx: DispatchContext, payload: BaseDispatchPayload) -> str | None:
    """Assess dispatch with view-refresh shortcut.

    If the active view variant already has data for the target, refresh it
    (for sectioned: UpdateView; for judgement: a fresh assess). Otherwise
    run a normal assess call.
    """
    assert isinstance(payload, AssessDispatchPayload)
    db = ctx.orchestrator.db
    view = get_active_view()
    if await view.exists(ctx.resolved_question_id, db):
        log.info(
            "Dispatch: assess redirected to view.refresh for %s — %s",
            ctx.d_label,
            payload.reason,
        )
        return await view.refresh(
            ctx.resolved_question_id,
            db,
            parent_call_id=ctx.parent_call_id,
            context_page_ids=payload.context_page_ids,
            broadcaster=ctx.orchestrator.broadcaster,
            force=ctx.force,
            call_id=ctx.call_id,
            sequence_id=ctx.sequence_id,
            sequence_position=ctx.sequence_position,
            pool_question_id=ctx.pool_question_id,
        )
    log.info("Dispatch: assess on %s — %s", ctx.d_label, payload.reason)
    return await assess_question(
        ctx.resolved_question_id,
        db,
        parent_call_id=ctx.parent_call_id,
        context_page_ids=payload.context_page_ids,
        broadcaster=ctx.orchestrator.broadcaster,
        force=ctx.force,
        call_id=ctx.call_id,
        sequence_id=ctx.sequence_id,
        sequence_position=ctx.sequence_position,
        summarise=ctx.orchestrator.summarise_before_assess,
        pool_question_id=ctx.pool_question_id,
    )


async def _handle_create_view(ctx: DispatchContext, payload: BaseDispatchPayload) -> str | None:
    from rumil.views.sectioned import create_view_for_question

    assert isinstance(payload, CreateViewDispatchPayload)
    log.info("Dispatch: create_view on %s — %s", ctx.d_label, payload.reason)
    return await create_view_for_question(
        ctx.resolved_question_id,
        ctx.orchestrator.db,
        parent_call_id=ctx.parent_call_id,
        context_page_ids=payload.context_page_ids,
        broadcaster=ctx.orchestrator.broadcaster,
        force=ctx.force,
        call_id=ctx.call_id,
        sequence_id=ctx.sequence_id,
        sequence_position=ctx.sequence_position,
    )


async def _handle_web_research(ctx: DispatchContext, payload: BaseDispatchPayload) -> str | None:
    assert isinstance(payload, WebResearchDispatchPayload)
    log.info("Dispatch: web_research on %s — %s", ctx.d_label, payload.reason)
    return await web_research_question(
        ctx.resolved_question_id,
        ctx.orchestrator.db,
        parent_call_id=ctx.parent_call_id,
        broadcaster=ctx.orchestrator.broadcaster,
        force=ctx.force,
        call_id=ctx.call_id,
        sequence_id=ctx.sequence_id,
        sequence_position=ctx.sequence_position,
        pool_question_id=ctx.pool_question_id,
    )


def _make_scout_handler(
    call_type: CallType,
    call_cls: type[CallRunner],
    log_label: str,
) -> DispatchHandler:
    """Factory for the 15 scope-only scout-family handlers.

    Each scout-family dispatch reduces to the same call:
    _run_simple_call_dispatch(resolved, call_type, call_cls, ...) with
    max_rounds/fruit_threshold carried from the payload. Only the
    (call_type, call_cls, log_label) triple varies between them.
    """

    async def handler(ctx: DispatchContext, payload: BaseDispatchPayload) -> str | None:
        assert isinstance(payload, MultiRoundFields)
        log.info(
            "Dispatch: %s on %s (max_rounds=%d) — %s",
            log_label,
            ctx.d_label,
            payload.max_rounds,
            payload.reason,
        )
        return await ctx.orchestrator._run_simple_call_dispatch(
            ctx.resolved_question_id,
            call_type,
            call_cls,
            ctx.parent_call_id,
            force=ctx.force,
            call_id=ctx.call_id,
            sequence_id=ctx.sequence_id,
            sequence_position=ctx.sequence_position,
            max_rounds=payload.max_rounds,
            fruit_threshold=payload.fruit_threshold,
        )

    return handler


DISPATCH_HANDLERS: dict[type[BaseDispatchPayload], DispatchHandler] = {
    ScoutDispatchPayload: _handle_find_considerations,
    AssessDispatchPayload: _handle_assess,
    CreateViewDispatchPayload: _handle_create_view,
    WebResearchDispatchPayload: _handle_web_research,
    ScoutSubquestionsDispatchPayload: _make_scout_handler(
        CallType.SCOUT_SUBQUESTIONS,
        ScoutSubquestionsCall,
        "scout_subquestions",
    ),
    ScoutEstimatesDispatchPayload: _make_scout_handler(
        CallType.SCOUT_ESTIMATES,
        ScoutEstimatesCall,
        "scout_estimates",
    ),
    ScoutHypothesesDispatchPayload: _make_scout_handler(
        CallType.SCOUT_HYPOTHESES,
        ScoutHypothesesCall,
        "scout_hypotheses",
    ),
    ScoutAnalogiesDispatchPayload: _make_scout_handler(
        CallType.SCOUT_ANALOGIES,
        ScoutAnalogiesCall,
        "scout_analogies",
    ),
    ScoutParadigmCasesDispatchPayload: _make_scout_handler(
        CallType.SCOUT_PARADIGM_CASES,
        ScoutParadigmCasesCall,
        "scout_paradigm_cases",
    ),
    ScoutFactchecksDispatchPayload: _make_scout_handler(
        CallType.SCOUT_FACTCHECKS,
        ScoutFactchecksCall,
        "scout_factchecks",
    ),
    ScoutWebQuestionsDispatchPayload: _make_scout_handler(
        CallType.SCOUT_WEB_QUESTIONS,
        ScoutWebQuestionsCall,
        "scout_web_questions",
    ),
    ScoutDeepQuestionsDispatchPayload: _make_scout_handler(
        CallType.SCOUT_DEEP_QUESTIONS,
        ScoutDeepQuestionsCall,
        "scout_deep_questions",
    ),
    ScoutCHowTrueDispatchPayload: _make_scout_handler(
        CallType.SCOUT_C_HOW_TRUE,
        ScoutCHowTrueCall,
        "scout_c_how_true",
    ),
    ScoutCHowFalseDispatchPayload: _make_scout_handler(
        CallType.SCOUT_C_HOW_FALSE,
        ScoutCHowFalseCall,
        "scout_c_how_false",
    ),
    ScoutCCruxesDispatchPayload: _make_scout_handler(
        CallType.SCOUT_C_CRUXES,
        ScoutCCruxesCall,
        "scout_c_cruxes",
    ),
    ScoutCRelevantEvidenceDispatchPayload: _make_scout_handler(
        CallType.SCOUT_C_RELEVANT_EVIDENCE,
        ScoutCRelevantEvidenceCall,
        "scout_c_relevant_evidence",
    ),
    ScoutCStressTestCasesDispatchPayload: _make_scout_handler(
        CallType.SCOUT_C_STRESS_TEST_CASES,
        ScoutCStressTestCasesCall,
        "scout_c_stress_test_cases",
    ),
    ScoutCRobustifyDispatchPayload: _make_scout_handler(
        CallType.SCOUT_C_ROBUSTIFY,
        ScoutCRobustifyCall,
        "scout_c_robustify",
    ),
    ScoutCStrengthenDispatchPayload: _make_scout_handler(
        CallType.SCOUT_C_STRENGTHEN,
        ScoutCStrengthenCall,
        "scout_c_strengthen",
    ),
}
