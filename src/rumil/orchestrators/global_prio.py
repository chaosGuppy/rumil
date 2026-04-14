"""
GlobalPrioOrchestrator: runs a global cross-cutting prioritiser
concurrently alongside a local tree-based prioritiser.

Each global turn has three phases:
  1. Explore — agent loop with graph-navigation tools
  2. Decide  — single LLM call: is there a cross-cutting question worth creating?
  3. Create  — single LLM call with create_subquestion + dispatch tools
"""

import asyncio
import logging
import math
from collections.abc import Sequence

from rumil.available_calls import get_available_calls_preset
from rumil.calls.common import (
    mark_call_completed,
    run_agent_loop,
    run_single_call,
)
from rumil.calls.dispatches import (
    DISPATCH_DEFS,
    filter_mode_schema,
    make_mode_validator,
)
from rumil.constants import (
    MAX_PROPAGATION_REASSESS,
    MIN_GLOBAL_PRIO_BUDGET,
    MIN_TWOPHASE_BUDGET,
)
from rumil.context import format_page
from rumil.database import DB
from rumil.llm import Tool, build_system_prompt
from rumil.models import (
    AssessDispatchPayload,
    Call,
    CallStatus,
    CallType,
    Dispatch,
    LinkType,
    MoveType,
    PageDetail,
    Workspace,
)
from rumil.moves.base import MoveState
from rumil.moves.registry import MOVES
from rumil.orchestrators.base import BaseOrchestrator
from rumil.orchestrators.common import assess_question
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import (
    ContextBuiltEvent,
    DispatchesPlannedEvent,
    DispatchTraceItem,
    ErrorEvent,
)
from rumil.tracing.tracer import CallTrace, set_trace
from rumil.workspace_exploration import (
    make_explore_subgraph_tool,
    make_load_page_tool,
    render_question_subgraph,
)

log = logging.getLogger(__name__)


class GlobalPrioOrchestrator(BaseOrchestrator):
    """Orchestrator that runs a global cross-cutting process alongside a local prioritiser.

    The local prioritiser (ExperimentalOrchestrator by default) handles
    tree-based investigation. The global process runs concurrently,
    exploring the full research graph for cross-cutting questions that
    benefit multiple branches, and propagating findings upward.
    """

    def __init__(
        self,
        db: DB,
        broadcaster: Broadcaster | None = None,
    ):
        super().__init__(db, broadcaster)
        self._global_consumed: int = 0
        self._global_cap: int = 0
        self._local_cap: int = 0
        self._action_history: list[dict] = []
        self._last_question_count: int = 0
        self._researched_question_ids: list[str] = []
        self._local_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._messages: list[dict] = []
        self._turn_count: int = 0

    async def run(self, root_question_id: str) -> None:
        own_db = await self.db.fork()
        self.db = own_db
        await self._setup()

        settings = get_settings()
        total, used = await self.db.get_budget()
        remaining = total - used

        global_cap = max(
            MIN_GLOBAL_PRIO_BUDGET,
            math.floor(remaining * settings.global_prio_budget_fraction),
        )
        local_cap = remaining - global_cap
        self._global_cap = global_cap
        self._local_cap = local_cap

        log.info(
            'GlobalPrioOrchestrator: total_remaining=%d, local_cap=%d, global_cap=%d',
            remaining, local_cap, global_cap,
        )

        if local_cap < MIN_TWOPHASE_BUDGET:
            log.warning(
                'Local budget too small (%d < %d), running global-only',
                local_cap, MIN_TWOPHASE_BUDGET,
            )
            local_cap = 0

        local = self._create_local_orchestrator(local_cap)
        self._last_question_count = await self.db.count_questions()

        try:
            if local and local_cap >= MIN_TWOPHASE_BUDGET:
                self._local_task = asyncio.create_task(
                    local.run(root_question_id),
                    name='global_prio_local',
                )
            global_task = asyncio.create_task(
                self._global_loop(root_question_id),
                name='global_prio_global',
            )

            tasks_to_await: list[asyncio.Task] = []  # type: ignore[type-arg]
            if self._local_task is not None:
                tasks_to_await.append(self._local_task)
            tasks_to_await.append(global_task)

            results = await asyncio.gather(*tasks_to_await, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    log.error('GlobalPrioOrchestrator task failed: %s', r, exc_info=r)
        finally:
            self._local_task = None
            await self._teardown()
            await own_db.close()

    def _create_local_orchestrator(
        self,
        budget_cap: int,
    ) -> BaseOrchestrator | None:
        """Create the local orchestrator based on settings."""
        settings = get_settings()
        variant = settings.global_prio_local_variant

        if variant == 'experimental':
            from rumil.orchestrators.experimental import ExperimentalOrchestrator
            orch = ExperimentalOrchestrator(
                self.db, self.broadcaster, budget_cap=budget_cap,
            )
            return orch
        if variant == 'two_phase':
            from rumil.orchestrators.two_phase import TwoPhaseOrchestrator
            orch = TwoPhaseOrchestrator(
                self.db, self.broadcaster, budget_cap=budget_cap,
            )
            return orch

        log.error('Unknown global_prio_local_variant: %s', variant)
        return None

    async def _global_loop(self, root_question_id: str) -> None:
        """Main loop for the global prioritisation process."""
        while self._global_consumed < self._global_cap:
            remaining_global = self._global_cap - self._global_consumed
            if remaining_global <= 0:
                break

            await self._wait_for_trigger()

            if self._global_consumed >= self._global_cap:
                break

            turn_result = await self._global_turn(root_question_id)

            if turn_result.get('dispatches'):
                dispatches = turn_result['dispatches']
                sequences: list[list[Dispatch]] = []
                for d in dispatches:
                    if d.payload.question_id == root_question_id:
                        sequences.append([d])
                    else:
                        assess = Dispatch(
                            call_type=CallType.ASSESS,
                            payload=AssessDispatchPayload(
                                question_id=d.payload.question_id,
                                reason='Auto-assess after global dispatch',
                            ),
                        )
                        sequences.append([d, assess])

                if sequences:
                    call_id = turn_result.get('call_id')
                    await self._run_sequences(sequences, root_question_id, call_id)
                    dispatched_count = sum(len(seq) for seq in sequences)
                    self._global_consumed += dispatched_count

            researched_ids = turn_result.get('researched_question_ids', [])
            if researched_ids:
                reassessed = await self._propagate_updates(
                    root_question_id, researched_ids,
                )
            else:
                reassessed = 0

            self._action_history.append({
                'created_questions': turn_result.get('created_questions', []),
                'dispatches_count': len(turn_result.get('dispatches', [])),
                'reassessed_count': reassessed,
            })

            log.info(
                'Global turn complete: dispatches=%d, reassessed=%d, '
                'consumed=%d/%d',
                len(turn_result.get('dispatches', [])),
                reassessed,
                self._global_consumed,
                self._global_cap,
            )

    async def _wait_for_trigger(self) -> None:
        """Wait until enough new questions exist or local task is done."""
        settings = get_settings()
        threshold = settings.global_prio_trigger_threshold

        if settings.is_smoke_test:
            threshold = 1

        while True:
            current_count = await self.db.count_questions()
            new_questions = current_count - self._last_question_count

            if new_questions >= threshold:
                self._last_question_count = current_count
                log.info(
                    'Global trigger: %d new questions (threshold=%d)',
                    new_questions, threshold,
                )
                return

            if self._local_task is not None and self._local_task.done():
                self._last_question_count = current_count
                log.info('Global trigger: local task completed')
                return

            await asyncio.sleep(2)

    async def _global_turn(
        self,
        root_question_id: str,
    ) -> dict:
        """Run one global prioritisation turn (explore → decide → create)."""
        remaining_global = self._global_cap - self._global_consumed

        p_call = await self.db.create_call(
            CallType.GLOBAL_PRIORITIZATION,
            scope_page_id=root_question_id,
            budget_allocated=remaining_global,
            workspace=Workspace.PRIORITIZATION,
        )
        trace = CallTrace(p_call.id, self.db, broadcaster=self.broadcaster)
        set_trace(trace)
        await trace.record(ContextBuiltEvent(budget=remaining_global))
        await self.db.update_call_status(p_call.id, CallStatus.RUNNING)

        state = MoveState(p_call, self.db)

        try:
            await self._explore_phase(root_question_id, p_call, trace, state)

            should_create, reasoning = await self._decide_phase(p_call, trace, state)

            if not should_create:
                await mark_call_completed(
                    p_call, self.db, 'No cross-cutting opportunity found',
                )
                self._turn_count += 1
                return {
                    'dispatches': [],
                    'call_id': p_call.id,
                    'created_questions': [],
                    'researched_question_ids': [],
                }

            create_result = await self._create_phase(
                reasoning, root_question_id, p_call, trace, state,
            )

            await trace.record(DispatchesPlannedEvent(
                dispatches=[
                    DispatchTraceItem(
                        call_type=d.call_type.value,
                        **d.payload.model_dump(exclude_defaults=True),
                    )
                    for d in create_result.get('dispatches', [])
                ],
            ))

            summary = (
                f"Global turn complete. "
                f"{len(create_result.get('dispatches', []))} dispatches, "
                f"{len(create_result.get('created_questions', []))} "
                f"cross-cutting questions created."
            )
            await mark_call_completed(p_call, self.db, summary)
            self._turn_count += 1
            return {
                **create_result,
                'call_id': p_call.id,
            }
        except Exception:
            log.exception('Global turn failed')
            await self.db.update_call_status(p_call.id, CallStatus.FAILED)
            await trace.record(ErrorEvent(message='Global turn failed'))
            self._turn_count += 1
            return {
                'dispatches': [],
                'call_id': p_call.id,
                'created_questions': [],
                'researched_question_ids': [],
            }

    async def _explore_phase(
        self,
        root_question_id: str,
        call: Call,
        trace: CallTrace,
        state: MoveState,
    ) -> None:
        """Phase 1: agent loop exploring the research graph with tools."""
        settings = get_settings()
        system_prompt = build_system_prompt('global_prio_explore')
        tools = [
            make_explore_subgraph_tool(
                self.db, trace, include_impact=True, questions_only=True,
            ),
            make_load_page_tool(self.db, trace),
        ]

        if self._turn_count == 0:
            root_page = await self.db.get_page(root_question_id)
            root_detail = ""
            if root_page:
                root_detail = await format_page(
                    root_page, PageDetail.CONTENT, db=self.db,
                )

            subgraph = await render_question_subgraph(
                root_question_id,
                self.db,
                max_pages=settings.global_prio_subgraph_max_pages,
                include_impact=True,
            )

            local_hint = await self._build_local_activity_hint()

            user_message = (
                "# Research Graph\n\n"
                "## Root Question\n\n"
                f"{root_detail}\n\n"
                "## Question Subgraph\n\n"
                f"{subgraph}\n\n"
                "## Local Prioritiser Activity\n\n"
                f"{local_hint}\n\n"
                "Explore the graph to identify cross-cutting research opportunities."
            )

            result = await run_agent_loop(
                system_prompt,
                user_message,
                tools,
                call_id=call.id,
                db=self.db,
                state=state,
                max_rounds=settings.global_prio_explore_rounds,
                cache=True,
            )
            self._messages = result.messages
        else:
            local_hint = await self._build_local_activity_hint()
            self._messages.append({
                "role": "user",
                "content": (
                    "## New Turn\n\n"
                    "The local prioritiser has continued working. "
                    "Here is updated activity:\n\n"
                    f"{local_hint}\n\n"
                    "Continue exploring the graph for cross-cutting opportunities. "
                    "Use explore_subgraph and load_page to investigate further."
                ),
            })

            result = await run_agent_loop(
                system_prompt,
                tools=tools,
                call_id=call.id,
                db=self.db,
                state=state,
                max_rounds=settings.global_prio_explore_rounds,
                messages=self._messages,
                cache=True,
            )
            self._messages = result.messages

    async def _decide_phase(
        self,
        call: Call,
        trace: CallTrace,
        state: MoveState,
    ) -> tuple[bool, str]:
        """Phase 2: decide whether a cross-cutting question is worth creating.

        Returns (should_create, reasoning_text).
        """
        last_assistant_text = self._extract_last_assistant_text()

        system_prompt = build_system_prompt('global_prio_decide')
        user_message = (
            "## Exploration Summary\n\n"
            f"{last_assistant_text}\n\n"
            "Based on your exploration, are there cross-cutting questions that, "
            "if answered, would substantially advance 2+ high-impact questions "
            "from different branches? Reply YES or NO, with brief reasoning."
        )

        result = await run_single_call(
            system_prompt,
            user_message,
            tools=[],
            call_id=call.id,
            phase="global_decide",
            db=self.db,
            state=state,
        )

        reasoning = result.text.strip()
        should_create = "YES" in reasoning.upper().split('\n')[0]
        log.info(
            'Global decide phase: should_create=%s, reasoning_len=%d',
            should_create, len(reasoning),
        )
        return should_create, reasoning

    async def _create_phase(
        self,
        decide_reasoning: str,
        root_question_id: str,
        call: Call,
        trace: CallTrace,
        state: MoveState,
    ) -> dict:
        """Phase 3: create cross-cutting question and dispatch research."""
        remaining_global = self._global_cap - self._global_consumed

        system_prompt = build_system_prompt('global_prio_create')
        user_message = (
            "## Decision\n\n"
            f"{decide_reasoning}\n\n"
            f"You have **{remaining_global} budget units** remaining.\n\n"
            "Create the cross-cutting question now. Use the create_subquestion "
            "tool to create it with links to the relevant parent questions, "
            "and dispatch research on it."
        )

        allowed_fc_modes = get_settings().allowed_find_considerations_modes
        state._dispatch_validators.append(make_mode_validator(allowed_fc_modes))

        tools: list[Tool] = []
        for mt in [MoveType.CREATE_SUBQUESTION, MoveType.LINK_CHILD_QUESTION]:
            tool = MOVES[mt].bind(state)
            if mt == MoveType.CREATE_SUBQUESTION:
                tool.input_schema = filter_mode_schema(
                    tool.input_schema, allowed_fc_modes,
                )
            tools.append(tool)

        dispatch_types = list(get_available_calls_preset().phase2_dispatch)
        for ct in dispatch_types:
            if ct in DISPATCH_DEFS:
                ddef = DISPATCH_DEFS[ct]
                tool = ddef.bind(
                    state,
                    scope_question_id=root_question_id,
                )
                if ddef.call_type == CallType.FIND_CONSIDERATIONS:
                    tool.input_schema = filter_mode_schema(
                        tool.input_schema, allowed_fc_modes,
                    )
                tools.append(tool)

        result = await run_single_call(
            system_prompt,
            user_message,
            tools,
            call_id=call.id,
            phase="global_create",
            db=self.db,
            state=state,
        )

        created_questions: list[dict] = []
        researched_question_ids: list[str] = []

        for d in state.dispatches:
            qid = d.payload.question_id
            if qid not in researched_question_ids:
                researched_question_ids.append(qid)

        for pid in state.created_page_ids:
            page = await self.db.get_page(pid)
            if page:
                links = await self.db.get_links_to(pid)
                parent_links = [
                    l for l in links if l.link_type == LinkType.CHILD_QUESTION
                ]
                created_questions.append({
                    'headline': page.headline,
                    'parent_count': len(parent_links),
                    'page_id': pid,
                })
                if pid not in researched_question_ids:
                    researched_question_ids.append(pid)

        return {
            'dispatches': list(state.dispatches),
            'created_questions': created_questions,
            'researched_question_ids': researched_question_ids,
        }

    def _extract_last_assistant_text(self) -> str:
        """Extract the last assistant text from the message stack."""
        for msg in reversed(self._messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    text_parts = [
                        b.get("text", "") if isinstance(b, dict) else str(b)
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    if text_parts:
                        return "\n".join(text_parts)
        return "(no exploration summary available)"

    async def _build_local_activity_hint(self) -> str:
        """Build a summary of recent local prioritiser activity."""
        lines: list[str] = []
        try:
            all_calls = await self.db.get_calls_for_run(self.db.run_id)
            recent = sorted(all_calls, key=lambda c: c.created_at, reverse=True)
            for c in recent[:20]:
                if c.call_type == CallType.GLOBAL_PRIORITIZATION:
                    continue
                label = (
                    await self.db.page_label(c.scope_page_id)
                    if c.scope_page_id else '?'
                )
                lines.append(f'- {c.call_type.value} on {label}')
        except Exception:
            log.debug('Could not build local activity hint', exc_info=True)
            return '(no activity data available)'

        if not lines:
            return '(no recent local activity)'
        return '\n'.join(lines)

    async def _propagate_updates(
        self,
        root_question_id: str,
        researched_question_ids: Sequence[str],
    ) -> int:
        """Re-assess parent questions of recently-researched cross-cutting questions.

        Returns the number of re-assess calls made.
        """
        parent_ids: set[str] = set()
        for qid in researched_question_ids:
            links = await self.db.get_links_to(qid)
            for link in links:
                if link.link_type == LinkType.CHILD_QUESTION:
                    parent_ids.add(link.from_page_id)

        parent_ids.discard(root_question_id)

        if not parent_ids:
            return 0

        staleness = await self.db.get_assess_staleness(list(parent_ids))
        stale_parents = [qid for qid, is_stale in staleness.items() if is_stale]

        if not stale_parents:
            return 0

        parent_impact: dict[str, int] = {}
        for qid in researched_question_ids:
            links = await self.db.get_links_to(qid)
            for link in links:
                if (
                    link.link_type == LinkType.CHILD_QUESTION
                    and link.from_page_id in stale_parents
                ):
                    impact = link.impact_on_parent_question or 5
                    parent_impact[link.from_page_id] = max(
                        parent_impact.get(link.from_page_id, 0), impact,
                    )

        sorted_parents = sorted(
            stale_parents,
            key=lambda q: parent_impact.get(q, 0),
            reverse=True,
        )

        reassessed = 0
        for qid in sorted_parents[:MAX_PROPAGATION_REASSESS]:
            if self._global_consumed >= self._global_cap:
                break
            try:
                call_id = await assess_question(
                    qid, self.db,
                    broadcaster=self.broadcaster,
                    force=True,
                )
                if call_id:
                    self._global_consumed += 1
                    reassessed += 1
                    log.info(
                        'Global propagation: re-assessed question %s',
                        qid[:8],
                    )
            except Exception:
                log.warning(
                    'Global propagation: failed to re-assess %s',
                    qid[:8], exc_info=True,
                )

        return reassessed
