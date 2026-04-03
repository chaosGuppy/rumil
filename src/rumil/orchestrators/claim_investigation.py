"""
ClaimInvestigationOrchestrator: two-phase orchestrator for investigating claims.
"""

import asyncio
import logging
from collections.abc import Sequence

from rumil.available_calls import get_available_calls_preset
from rumil.calls.common import mark_call_completed
from rumil.calls.dispatches import (
    DISPATCH_DEFS,
    DispatchDef,
    RECURSE_CLAIM_DISPATCH_DEF,
    RECURSE_DISPATCH_DEF,
)
from rumil.calls.prioritization import run_prioritization_call
from rumil.constants import LAST_CALL_THRESHOLD, MIN_TWOPHASE_BUDGET
from rumil.context import build_prioritization_context, collect_subtree_ids
from rumil.database import DB
from rumil.llm import (
    LLMExchangeMetadata,
    build_system_prompt,
    build_user_message,
    structured_call,
)
from rumil.models import (
    AssessDispatchPayload,
    Call,
    CallType,
    Dispatch,
    LinkType,
    RecurseClaimDispatchPayload,
    RecurseDispatchPayload,
    Workspace,
)
from rumil.orchestrators.base import BaseOrchestrator
from rumil.orchestrators.common import (
    CallTypeFruitScore,
    ClaimScore,
    ClaimScoringResult,
    PerTypeFruitResult,
    PrioritizationResult,
    SubquestionScore,
    SubquestionScoringResult,
    _describe_considerations_on_page,
    assess_question,
    compute_dispatch_guidance,
)
from rumil.page_graph import PageGraph
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import (
    CallTypeFruitScoreItem,
    ClaimScoreItem,
    ContextBuiltEvent,
    DispatchExecutedEvent,
    DispatchesPlannedEvent,
    DispatchTraceItem,
    ErrorEvent,
    ScoringCompletedEvent,
)
from rumil.tracing.tracer import CallTrace, set_trace


log = logging.getLogger(__name__)


class ClaimInvestigationOrchestrator(BaseOrchestrator):
    """Two-phase orchestrator for investigating claims.

    Phase 1: Fan out with claim-specific scouts (how-true, how-false,
    cruxes, relevant-evidence, stress-test-cases).
    Phase 2: Score identified items for impact and remaining fruit,
    then dispatch targeted follow-up (more scouts, find_considerations,
    recurse into claim or question investigation).
    """

    def __init__(
        self, db: DB,
        broadcaster: Broadcaster | None = None,
        budget_cap: int | None = None,
    ):
        super().__init__(db, broadcaster)
        self._invocation: int = 0
        self._call_id: str | None = None

        self._executed_since_last_plan: bool = False
        self._budget_cap: int | None = budget_cap
        self._consumed: int = 0
        self._initial_call: Call | None = None
        self._parent_call_id: str | None = None
        self._sequence_id: str | None = None
        self._seq_position: int = 0

    def _effective_budget(self, global_remaining: int) -> int:
        if self._budget_cap is not None:
            return min(global_remaining, self._budget_cap - self._consumed)
        return global_remaining

    async def _pacing_params(self) -> tuple[int, int]:
        if self._budget_cap is not None:
            return self._budget_cap, self._consumed
        return await self.db.get_budget()

    async def create_initial_call(
        self,
        claim_id: str,
        parent_call_id: str | None = None,
    ) -> str:
        """Eagerly create the phase-1 prioritization call record."""
        budget = self._effective_budget(await self.db.budget_remaining())
        budget = await self._paced_budget(budget)
        phase1_budget = budget
        p_call = await self.db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=claim_id,
            parent_call_id=parent_call_id,
            budget_allocated=phase1_budget,
            workspace=Workspace.PRIORITIZATION,
        )
        self._call_id = p_call.id
        self._initial_call = p_call
        self._parent_call_id = parent_call_id
        return p_call.id

    async def run(self, root_question_id: str) -> None:
        claim_id = root_question_id
        own_db = await self.db.fork()
        self.db = own_db
        await self._setup()
        remaining = await self.db.budget_remaining()
        effective = self._effective_budget(remaining)
        if effective < MIN_TWOPHASE_BUDGET:
            raise ValueError(
                f'ClaimInvestigationOrchestrator requires a budget of at least '
                f'{MIN_TWOPHASE_BUDGET}, got {effective}'
            )
        if self._parent_call_id:
            seq = await self.db.create_call_sequence(
                parent_call_id=self._parent_call_id,
                scope_question_id=claim_id,
            )
            self._sequence_id = seq.id
            self._seq_position = 0
        try:
            while True:
                remaining = await self.db.budget_remaining()
                effective = self._effective_budget(remaining)
                if effective <= 0:
                    break

                last_call = effective < LAST_CALL_THRESHOLD
                if last_call:
                    round_budget = effective
                else:
                    round_budget = await self._paced_budget(effective)
                result = await self._get_next_batch(
                    claim_id, round_budget, total_remaining=effective,
                    last_call=last_call,
                )
                if not result.dispatch_sequences and not result.children:
                    break

                tasks: list = []
                if result.dispatch_sequences:
                    tasks.append(self._run_sequences(
                        result.dispatch_sequences, claim_id,
                        result.call_id,
                    ))
                for child, child_id in result.children:
                    tasks.append(child.run(child_id))

                if not tasks:
                    break

                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        log.error('Concurrent dispatch failed: %s', r, exc_info=r)
                        if result.call_id:
                            trace = CallTrace(
                                result.call_id, self.db,
                                broadcaster=self.broadcaster,
                            )
                            await trace.record(ErrorEvent(
                                message=(
                                    f"Concurrent dispatch failed: "
                                    f"{type(r).__name__}: {r}"
                                ),
                                phase="dispatch",
                            ))

                if not any(not isinstance(r, Exception) for r in results):
                    break

                self._executed_since_last_plan = True

                if self._invocation > 1 or last_call:
                    await assess_question(
                        claim_id, self.db,
                        parent_call_id=self._parent_call_id,
                        broadcaster=self.broadcaster, force=True,
                        sequence_id=self._sequence_id,
                        sequence_position=self._seq_position,
                    )
                    if self._sequence_id is not None:
                        self._seq_position += 2

                if last_call:
                    break
        finally:
            await self._teardown()
            await own_db.close()

    async def _run_dispatch_sequence(
        self,
        sequence: Sequence[Dispatch],
        scope_question_id: str,
        parent_call_id: str | None,
        base_index: int,
        position_in_batch: int = 0,
    ) -> bool:
        result = await super()._run_dispatch_sequence(
            sequence, scope_question_id, parent_call_id, base_index,
            position_in_batch=position_in_batch,
        )
        if result:
            self._consumed += len(sequence)
        return result

    async def _is_new_claim(self, claim_id: str) -> bool:
        """A claim is 'new' if it has no consideration links to it."""
        links = await self.db.get_links_to(claim_id)
        return not any(
            l.link_type == LinkType.CONSIDERATION for l in links
        )

    async def _cancel_initial_call(self) -> None:
        if self._initial_call is None:
            return
        call = self._initial_call
        self._initial_call = None
        if self._sequence_id is not None:
            call.sequence_id = self._sequence_id
            call.sequence_position = self._seq_position
            await self.db.save_call(call)
            self._seq_position += 1
        await mark_call_completed(
            call, self.db, 'Phase 1 skipped — claim already has research.',
        )

    async def _get_next_batch(
        self,
        claim_id: str,
        budget: int,
        parent_call_id: str | None = None,
        total_remaining: int | None = None,
        last_call: bool = False,
    ) -> 'PrioritizationResult':

        if self._invocation == 0:
            self._invocation += 1
            if await self._is_new_claim(claim_id):
                return await self._phase1(
                    claim_id, budget, parent_call_id,
                    total_remaining=total_remaining,
                    last_call=last_call,
                )
            await self._cancel_initial_call()
            self._executed_since_last_plan = True

        if not self._executed_since_last_plan:
            return PrioritizationResult(dispatch_sequences=[])

        self._executed_since_last_plan = False
        self._invocation += 1
        return await self._phase2(
            claim_id, budget, self._parent_call_id,
            total_remaining=total_remaining,
            last_call=last_call,
        )

    async def _phase1(
        self,
        claim_id: str,
        budget: int,
        parent_call_id: str | None,
        total_remaining: int | None = None,
        last_call: bool = False,
    ) -> 'PrioritizationResult':

        phase1_budget = budget
        log.info(
            'ClaimInvestigationOrchestrator phase1: claim=%s, budget=%d, phase1_budget=%d',
            claim_id[:8], budget, phase1_budget,
        )

        graph = await PageGraph.load(self.db)
        context_text, short_id_map = await build_prioritization_context(
            self.db, scope_question_id=claim_id, graph=graph,
        )
        subtree_ids = await collect_subtree_ids(claim_id, self.db, graph=graph)
        if self._initial_call is not None:
            p_call = self._initial_call
            self._initial_call = None
            if self._sequence_id is not None:
                p_call.sequence_id = self._sequence_id
                p_call.sequence_position = self._seq_position
                await self.db.save_call(p_call)
                self._seq_position += 1
        else:
            p_call = await self.db.create_call(
                CallType.PRIORITIZATION,
                scope_page_id=claim_id,
                parent_call_id=parent_call_id,
                budget_allocated=phase1_budget,
                workspace=Workspace.PRIORITIZATION,
                sequence_id=self._sequence_id,
                sequence_position=self._seq_position if self._sequence_id else None,
            )
            if self._sequence_id is not None:
                self._seq_position += 1
        trace = CallTrace(p_call.id, self.db, broadcaster=self.broadcaster)
        set_trace(trace)
        await trace.record(ContextBuiltEvent(budget=phase1_budget))

        budget_line = (
            f'You have a budget of **{phase1_budget} research calls** to distribute '
            'among the dispatch tools below.'
        )
        if last_call:
            budget_line += (
                ' **This is your FINAL allocation — there will be no further '
                'research rounds after this. Spend the full budget on the '
                'highest-value work.**'
            )
        elif total_remaining is not None and total_remaining > phase1_budget:
            budget_line += (
                f' The overall question has **{total_remaining} budget remaining** '
                'across future rounds.'
            )
        task = (
            f'{budget_line}\n\n'
            f'Scope claim ID: `{claim_id}`\n\n'
            'Your job is to call the dispatch tools to fan out exploratory research on '
            'this claim. All scout dispatches automatically target the scope claim. '
            'You MUST call at least one dispatch tool right now — this is '
            'your only turn and you will not get another chance. Distribute your budget '
            'among the scouting dispatch tools, weighting towards types that seem most '
            'useful for this claim and skipping types that are clearly irrelevant. '
            'Do not do anything else — just dispatch.'
        )

        result = await run_prioritization_call(
            task, context_text, p_call, self.db,

            subtree_ids=subtree_ids,
            short_id_map=short_id_map,
            dispatch_types=list(get_available_calls_preset().claim_phase1_scouts),
            system_prompt_override=build_system_prompt('claim_investigation_p1'),
        )

        dispatches = list(result.dispatches)
        if not dispatches:
            log.warning(
                'Phase 1 produced no dispatches, synthesizing default scouts '
                'for claim=%s', claim_id[:8],
            )
            preset = get_available_calls_preset()
            for ct in preset.claim_phase1_scouts[:phase1_budget]:
                ddef = DISPATCH_DEFS[ct]
                dispatches.append(Dispatch(
                    call_type=ct,
                    payload=ddef.schema(
                        question_id=claim_id,
                        reason='fallback — phase 1 produced no dispatches',
                    ),
                ))
        sequences: list[list[Dispatch]] = [[d] for d in dispatches]

        await trace.record(DispatchesPlannedEvent(
            dispatches=[
                DispatchTraceItem(
                    call_type=d.call_type.value,
                    **d.payload.model_dump(exclude_defaults=True),
                )
                for d in dispatches
            ],
        ))

        await mark_call_completed(
            p_call, self.db,
            f'Phase 1 complete. Planned {len(sequences)} concurrent sequences.',
        )

        self._call_id = p_call.id

        log.info(
            'ClaimInvestigationOrchestrator phase1 complete: %d sequences',
            len(sequences),
        )
        return PrioritizationResult(
            dispatch_sequences=sequences,
            call_id=p_call.id,
        )

    async def _phase2(
        self,
        claim_id: str,
        budget: int,
        parent_call_id: str | None,
        total_remaining: int | None = None,
        last_call: bool = False,
    ) -> 'PrioritizationResult':
        from rumil.orchestrators.common import PrioritizationResult
        from rumil.orchestrators.two_phase import TwoPhaseOrchestrator

        log.info(
            'ClaimInvestigationOrchestrator phase2: claim=%s, budget=%d, last_call=%s',
            claim_id[:8], budget, last_call,
        )

        p_call = await self.db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=claim_id,
            parent_call_id=parent_call_id,
            budget_allocated=budget,
            workspace=Workspace.PRIORITIZATION,
            sequence_id=self._sequence_id,
            sequence_position=self._seq_position if self._sequence_id else None,
        )
        if self._sequence_id is not None:
            self._seq_position += 1
        trace = CallTrace(p_call.id, self.db, broadcaster=self.broadcaster)
        set_trace(trace)
        await trace.record(ContextBuiltEvent(budget=budget))

        graph = await PageGraph.load(self.db)
        scope_page = await graph.get_page(claim_id)
        if not scope_page:
            raise RuntimeError(
                f'Scope claim {claim_id} not found in PageGraph.'
            )
        scope_headline = scope_page.headline

        scoring_system = build_system_prompt('score_claim_items')

        claims_text, questions_text = await _describe_considerations_on_page(
            claim_id, graph,
        )

        scoring_tasks = []

        items_to_score = (
            f'Scope claim: {scope_headline}\n\n'
            f'## Claims (how-true stories, how-false stories, cruxes)\n{claims_text}\n\n'
            f'## Questions (evidence questions, stress-test cases, crux-questions)\n{questions_text}'
        )
        scoring_tasks.append(structured_call(
            scoring_system,
            user_message=build_user_message(
                items_to_score,
                'Score each item on impact and fruit.',
            ),
            response_model=ClaimScoringResult,
            metadata=LLMExchangeMetadata(
                call_id=p_call.id,
                phase='score_claim_items',
            ),
            db=self.db,
        ))

        preset = get_available_calls_preset()
        scout_types = [
            ct for ct in preset.claim_phase2_dispatch
            if ct.value.startswith('scout_c_')
        ]
        type_desc_lines = [
            '- **development**: Deeper investigation of existing claims and '
            'questions via find_considerations, web_research, and recursion.',
        ]
        for ct in scout_types:
            ddef = DISPATCH_DEFS.get(ct)
            if ddef:
                type_desc_lines.append(f'- **{ct.value}**: {ddef.description}')
        type_descriptions = '\n'.join(type_desc_lines)

        call_counts = await self.db.get_call_counts_by_type(claim_id)
        history_lines = [f'- {ct}: {n} call(s)' for ct, n in call_counts.items()]
        history_text = (
            'Prior completed calls on this claim:\n'
            + ('\n'.join(history_lines) if history_lines else '(none)')
        )

        fruit_system = build_system_prompt('score_per_type_fruit')
        fruit_user_msg = build_user_message(
            f'Claim: {scope_headline}\n\n'
            f'Claim ID: `{claim_id}`\n\n'
            f'{history_text}\n\n'
            f'## Call types to score\n\n{type_descriptions}',
            'Score the remaining fruit for each call type listed. '
            'Return one score per call type.',
        )
        scoring_tasks.append(structured_call(
            fruit_system,
            user_message=fruit_user_msg,
            response_model=PerTypeFruitResult,
            metadata=LLMExchangeMetadata(
                call_id=p_call.id,
                phase='score_per_type_fruit',
            ),
            db=self.db,
        ))

        scoring_results = await asyncio.gather(*scoring_tasks)
        item_result = scoring_results[0]
        fruit_result = scoring_results[1]

        item_scores = item_result.data.get('scores', []) if item_result.data else []
        raw_fruit_scores = fruit_result.data.get('scores', []) if fruit_result.data else []
        per_type_scores = [CallTypeFruitScore(**s) for s in raw_fruit_scores]

        has_dev_score = any(s.call_type == 'development' for s in per_type_scores)
        if not has_dev_score:
            log.warning(
                'LLM did not return a development fruit score; defaulting to 5'
            )
            await trace.record(ErrorEvent(
                message='LLM omitted development fruit score; defaulting to 5',
                phase='score_per_type_fruit',
            ))

        guidance = compute_dispatch_guidance(per_type_scores)

        await trace.record(ScoringCompletedEvent(
            claim_scores=[
                ClaimScoreItem(**s) for s in item_scores
            ],
            per_type_fruit=[
                CallTypeFruitScoreItem(
                    call_type=s.call_type, fruit=s.fruit, reasoning=s.reasoning,
                )
                for s in per_type_scores
            ],
            dispatch_guidance=guidance,
        ))

        scores_text = ''
        if item_scores:
            lines = ['## Item Scores', '']
            for s in item_scores:
                pid = s.get('page_id', s.get('question_id', '?'))
                lines.append(
                    f'- `{pid}` — {s.get("headline", "")}: '
                    f'impact={s.get("impact", 0)}, fruit={s.get("fruit", 0)} '
                    f'({s.get("reasoning", "")})'
                )
            lines.append('')
            scores_text = '\n'.join(lines)

        fruit_lines = ['## Per-Scout-Type Fruit Scores', '']
        for s in per_type_scores:
            fruit_lines.append(
                f'- **{s.call_type}**: {s.fruit}/10 — {s.reasoning}'
            )
        fruit_lines.append('')
        scores_text += '\n'.join(fruit_lines)

        if guidance:
            scores_text += f'\n## Dispatch Guidance\n\n{guidance}\n'

        context_text, short_id_map = await build_prioritization_context(
            self.db, scope_question_id=claim_id, graph=graph,
        )
        subtree_ids = await collect_subtree_ids(claim_id, self.db, graph=graph)

        budget_line = f'You have a budget of **{budget} budget units** to allocate.'
        if last_call:
            budget_line += (
                ' **This is your FINAL allocation — there will be no further '
                'research rounds after this. Spend the full budget on the '
                'highest-value remaining work.**'
            )
        elif total_remaining is not None and total_remaining > budget:
            budget_line += (
                f' The overall question has **{total_remaining} budget remaining** '
                'across future rounds.'
            )
        task = (
            f'{budget_line}\n\n'
            f'Scope claim ID: `{claim_id}`\n\n'
            f'{scores_text}\n\n'
            'You must make all your dispatch calls now — this is your only turn. '
            f'Each recurse call must have a budget of at least {MIN_TWOPHASE_BUDGET}.'
        )

        extra_defs: list[DispatchDef] = []
        if budget >= MIN_TWOPHASE_BUDGET:
            extra_defs.append(RECURSE_CLAIM_DISPATCH_DEF)
            extra_defs.append(RECURSE_DISPATCH_DEF)

        result = await run_prioritization_call(
            task, context_text, p_call, self.db,

            subtree_ids=subtree_ids,
            short_id_map=short_id_map,
            dispatch_types=list(get_available_calls_preset().claim_phase2_dispatch),
            extra_dispatch_defs=extra_defs or None,
            system_prompt_override=build_system_prompt('claim_investigation_p2'),
        )

        sequences: list[list[Dispatch]] = []
        children: list[tuple[ClaimInvestigationOrchestrator | TwoPhaseOrchestrator, str]] = []
        for d in result.dispatches:
            if isinstance(d.payload, RecurseClaimDispatchPayload):
                resolved = await self.db.resolve_page_id(d.payload.question_id)
                if not resolved:
                    log.warning(
                        'Recurse claim ID not found: %s',
                        d.payload.question_id[:8],
                    )
                    continue
                child = ClaimInvestigationOrchestrator(
                    self.db, self.broadcaster, budget_cap=d.payload.budget,
                )
                child._parent_call_id = p_call.id
                children.append((child, resolved))
                log.info(
                    'Queued recursive claim investigation: claim=%s, budget=%d — %s',
                    resolved[:8], d.payload.budget, d.payload.reason,
                )
            elif isinstance(d.payload, RecurseDispatchPayload):
                resolved = await self.db.resolve_page_id(d.payload.question_id)
                if not resolved:
                    log.warning(
                        'Recurse question ID not found: %s',
                        d.payload.question_id[:8],
                    )
                    continue
                child = TwoPhaseOrchestrator(
                    self.db, self.broadcaster, budget_cap=d.payload.budget,
                )
                child._parent_call_id = p_call.id
                children.append((child, resolved))
                log.info(
                    'Queued recursive question investigation: question=%s, budget=%d — %s',
                    resolved[:8], d.payload.budget, d.payload.reason,
                )
            elif d.payload.question_id == claim_id:
                sequences.append([d])
            else:
                assess = Dispatch(
                    call_type=CallType.ASSESS,
                    payload=AssessDispatchPayload(
                        question_id=d.payload.question_id,
                        reason='Auto-assess after phase-2 dispatch',
                    ),
                )
                sequences.append([d, assess])

        all_dispatches = [d for seq in sequences for d in seq]
        all_trace_items = [
            DispatchTraceItem(
                call_type=d.call_type.value,
                **d.payload.model_dump(exclude_defaults=True),
            )
            for d in all_dispatches
        ]
        for d in result.dispatches:
            if isinstance(d.payload, (RecurseClaimDispatchPayload, RecurseDispatchPayload)):
                all_trace_items.append(DispatchTraceItem(
                    call_type='recurse',
                    **d.payload.model_dump(exclude_defaults=True),
                ))
        await trace.record(DispatchesPlannedEvent(dispatches=all_trace_items))

        recurse_base = len(all_dispatches)
        for ci, (child, child_id) in enumerate(children):
            child_call_id = await child.create_initial_call(
                child_id, parent_call_id=p_call.id,
            )
            child_page = await self.db.get_page(child_id)
            await trace.record(DispatchExecutedEvent(
                index=recurse_base + ci,
                child_call_type='recurse',
                question_id=child_id,
                question_headline=child_page.headline if child_page else '',
                child_call_id=child_call_id,
            ))

        await mark_call_completed(
            p_call, self.db,
            f'Phase 2 complete. Planned {len(sequences)} concurrent sequences, '
            f'{len(children)} recursive children.',
        )

        self._call_id = p_call.id

        log.info(
            'ClaimInvestigationOrchestrator phase2 complete: %d sequences, %d children',
            len(sequences), len(children),
        )
        return PrioritizationResult(
            dispatch_sequences=sequences,
            call_id=p_call.id,
            children=children,
        )
