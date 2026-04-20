"""
GlobalPrioOrchestrator: runs a global cross-cutting prioritiser
concurrently alongside a local tree-based prioritiser.

Each global turn has up to four phases:
  1. Explore  — agent loop with graph-navigation tools
  2. Decide   — structured output: is there a cross-cutting question worth creating?
  3. Create   — single LLM call to create the question with multi-parent links
  4. Dispatch — one LLM call per created question to dispatch research (concurrent)
"""

import asyncio
import heapq
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from rumil.calls.common import (
    mark_call_completed,
    prepare_tools,
    run_agent_loop,
    run_single_call,
)
from rumil.calls.dispatches import (
    DISPATCH_DEFS,
    RECURSE_DISPATCH_DEF,
)
from rumil.constants import (
    MAX_PROPAGATION_REASSESS,
    MIN_GLOBAL_PRIO_BUDGET,
    MIN_TWOPHASE_BUDGET,
)
from rumil.context import format_page
from rumil.database import DB
from rumil.llm import (
    LLMExchangeMetadata,
    Tool,
    build_system_prompt,
    structured_call,
)
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    Dispatch,
    LinkType,
    MoveType,
    PageDetail,
    RecurseDispatchPayload,
    Workspace,
)
from rumil.moves.base import MoveState
from rumil.moves.registry import MOVES
from rumil.orchestrators.base import BaseOrchestrator
from rumil.orchestrators.common import assess_question
from rumil.orchestrators.experimental import ExperimentalOrchestrator
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import (
    ContextBuiltEvent,
    DispatchesPlannedEvent,
    DispatchExecutedEvent,
    DispatchTraceItem,
    ErrorEvent,
    GlobalPhaseCompletedEvent,
)
from rumil.tracing.tracer import CallTrace, set_trace
from rumil.workspace_exploration import (
    make_explore_subgraph_tool,
    make_load_page_tool,
    render_question_subgraph,
)

log = logging.getLogger(__name__)

_INF = float("inf")


class GlobalDecideResult(BaseModel):
    should_create: bool = Field(description="True if a cross-cutting question is worth creating.")
    reasoning: str = Field(description="Brief explanation of the decision.")
    question_headline: str | None = Field(
        None,
        description=(
            "If should_create is true, the proposed headline for the "
            "cross-cutting question (10-15 words)."
        ),
    )
    parent_question_ids: list[str] = Field(
        default_factory=list,
        description=(
            "If should_create is true, the short IDs of the 2+ parent "
            "questions this would feed into."
        ),
    )


@dataclass
class _PropagationPath:
    """A path from a researched question to the root, with its total weight."""

    nodes: list[str]
    weight: float


def _dijkstra_distances(
    graph: dict[str, dict[str, float]],
    source: str,
) -> dict[str, float]:
    """Single-source Dijkstra returning shortest distance to all reachable nodes."""
    dist: dict[str, float] = {source: 0.0}
    visited: set[str] = set()
    heap: list[tuple[float, str]] = [(0.0, source)]

    while heap:
        d, u = heapq.heappop(heap)
        if u in visited:
            continue
        visited.add(u)
        for neighbor, w in graph.get(u, {}).items():
            alt = d + w
            if alt < dist.get(neighbor, _INF):
                dist[neighbor] = alt
                heapq.heappush(heap, (alt, neighbor))

    return dist


def _dijkstra(
    graph: dict[str, dict[str, float]],
    source: str,
    target: str,
    excluded_nodes: set[str] | None = None,
    excluded_edges: set[tuple[str, str]] | None = None,
) -> _PropagationPath | None:
    """Shortest path from *source* to *target* in the child→parent graph.

    Edges go child→parent. Supports node and edge exclusions for Yen's
    algorithm. Returns None if no path exists.
    """
    excl_n = excluded_nodes or set()
    excl_e = excluded_edges or set()

    dist: dict[str, float] = {source: 0.0}
    prev: dict[str, str] = {}
    visited: set[str] = set()
    heap: list[tuple[float, str]] = [(0.0, source)]

    while heap:
        d, u = heapq.heappop(heap)
        if u in visited:
            continue
        visited.add(u)
        if u == target:
            path = []
            node = target
            while node != source:
                path.append(node)
                node = prev[node]
            path.append(source)
            path.reverse()
            return _PropagationPath(nodes=path, weight=d)

        for neighbor, w in graph.get(u, {}).items():
            if neighbor in excl_n or (u, neighbor) in excl_e:
                continue
            alt = d + w
            if alt < dist.get(neighbor, _INF):
                dist[neighbor] = alt
                prev[neighbor] = u
                heapq.heappush(heap, (alt, neighbor))

    return None


def _yen_k_shortest(
    graph: dict[str, dict[str, float]],
    source: str,
    target: str,
    k: int,
) -> list[_PropagationPath]:
    """Yen's algorithm: find up to *k* shortest simple paths source→target."""
    best = _dijkstra(graph, source, target)
    if best is None:
        return []

    A: list[_PropagationPath] = [best]
    B: list[_PropagationPath] = []

    for ki in range(1, k):
        prev_path = A[ki - 1]
        for i in range(len(prev_path.nodes) - 1):
            spur_node = prev_path.nodes[i]
            root_path = prev_path.nodes[: i + 1]

            excluded_edges: set[tuple[str, str]] = set()
            for p in A:
                if p.nodes[: i + 1] == root_path:
                    excluded_edges.add((p.nodes[i], p.nodes[i + 1]))

            excluded_nodes = set(root_path[:-1])

            spur = _dijkstra(
                graph,
                spur_node,
                target,
                excluded_nodes=excluded_nodes,
                excluded_edges=excluded_edges,
            )
            if spur is None:
                continue

            candidate = _PropagationPath(
                nodes=root_path[:-1] + spur.nodes,
                weight=_path_weight(graph, root_path[:-1] + spur.nodes),
            )
            if not any(c.nodes == candidate.nodes for c in B):
                B.append(candidate)

        if not B:
            break
        B.sort(key=lambda p: p.weight)
        A.append(B.pop(0))

    return A


def _path_weight(
    graph: dict[str, dict[str, float]],
    nodes: Sequence[str],
) -> float:
    total = 0.0
    for i in range(len(nodes) - 1):
        total += graph.get(nodes[i], {}).get(nodes[i + 1], _INF)
    return total


def _find_propagation_paths(
    graph: dict[str, dict[str, float]],
    sources: Sequence[str],
    target: str,
    k_per_source: int = 3,
) -> list[_PropagationPath]:
    """Find highest-impact paths from any source to target.

    Returns paths sorted by weight (lowest = highest impact), deduplicated.
    """
    all_paths: list[_PropagationPath] = []
    seen: set[tuple[str, ...]] = set()
    for src in sources:
        if src == target:
            continue
        for p in _yen_k_shortest(graph, src, target, k_per_source):
            key = tuple(p.nodes)
            if key not in seen:
                seen.add(key)
                all_paths.append(p)
    all_paths.sort(key=lambda p: p.weight)
    return all_paths


def _select_nodes_from_paths(
    paths: Sequence[_PropagationPath],
    budget: int = MAX_PROPAGATION_REASSESS,
) -> list[str]:
    """Greedily select nodes to re-assess from sorted propagation paths.

    The best path is always taken in full regardless of budget. Subsequent
    paths are taken only if their *new* intermediate nodes (not already
    selected by a prior path) fit within the remaining budget. If a path
    doesn't fully fit, as many of its new nodes as the budget allows are
    taken (bottom-up order). The source endpoint is excluded (it was
    just researched); the target (root) is included.
    """
    if not paths:
        return []

    selected: list[str] = []
    selected_set: set[str] = set()

    for i, path in enumerate(paths):
        new_nodes = [n for n in path.nodes[1:] if n not in selected_set]
        if not new_nodes:
            continue

        if i == 0 or len(new_nodes) <= budget - len(selected):
            take = new_nodes
        else:
            remaining = budget - len(selected)
            if remaining <= 0:
                break
            take = new_nodes[:remaining]

        for n in take:
            selected.append(n)
            selected_set.add(n)

        if len(selected) >= budget:
            break

    return selected


async def compute_global_impacts(
    root_question_id: str,
    db: DB,
    max_depth: int = 20,
) -> dict[str, float]:
    """Compute global impact scores for all questions reachable from root.

    BFS downward from root via CHILD_QUESTION links, building a weighted
    graph (parent->child, weight = -log(impact/10)). Single-source Dijkstra
    from root gives the highest-impact path to each node.

    Returns {page_id: impact_score} where impact_score is on a 0-10 scale.
    O(depth) DB round trips.
    """
    graph: dict[str, dict[str, float]] = {}
    visited: set[str] = {root_question_id}
    frontier = [root_question_id]

    for _ in range(max_depth):
        if not frontier:
            break

        outgoing = await db.get_links_from_many(frontier)

        next_frontier: list[str] = []
        for parent_id in frontier:
            for link in outgoing.get(parent_id, []):
                if link.link_type != LinkType.CHILD_QUESTION:
                    continue
                child_id = link.to_page_id
                impact = link.impact_on_parent_question or 5
                weight = -math.log(max(impact, 1) / 10.0)

                if parent_id not in graph:
                    graph[parent_id] = {}
                graph[parent_id][child_id] = weight

                if child_id not in visited:
                    visited.add(child_id)
                    next_frontier.append(child_id)

        frontier = next_frontier

    distances = _dijkstra_distances(graph, root_question_id)

    impacts: dict[str, float] = {}
    for node_id, dist in distances.items():
        if node_id == root_question_id:
            continue
        impacts[node_id] = round(10.0 * math.exp(-dist), 1)

    return impacts


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
        self._last_trigger_at: datetime = datetime.now(UTC)
        self._researched_question_ids: list[str] = []
        self._local_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._messages: list[dict] = []
        self._turn_count: int = 0

        variant = get_settings().prioritizer_variant
        if variant == "experimental":
            self.summarise_before_assess = ExperimentalOrchestrator.summarise_before_assess
        elif variant == "two_phase":
            self.summarise_before_assess = TwoPhaseOrchestrator.summarise_before_assess

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
            "GlobalPrioOrchestrator: total_remaining=%d, local_cap=%d, global_cap=%d",
            remaining,
            local_cap,
            global_cap,
        )

        if local_cap < MIN_TWOPHASE_BUDGET:
            log.warning(
                "Local budget too small (%d < %d), running global-only",
                local_cap,
                MIN_TWOPHASE_BUDGET,
            )
            local_cap = 0

        local = self._create_local_orchestrator(local_cap)
        self._last_trigger_at = datetime.now(UTC)

        try:
            if local and local_cap >= MIN_TWOPHASE_BUDGET:
                self._local_task = asyncio.create_task(
                    local.run(root_question_id),
                    name="global_prio_local",
                )
            global_task = asyncio.create_task(
                self._global_loop(root_question_id),
                name="global_prio_global",
            )

            tasks_to_await: list[asyncio.Task] = []  # type: ignore[type-arg]
            if self._local_task is not None:
                tasks_to_await.append(self._local_task)
            tasks_to_await.append(global_task)

            results = await asyncio.gather(*tasks_to_await, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    log.error("GlobalPrioOrchestrator task failed: %s", r, exc_info=r)
        finally:
            self._local_task = None
            await self._teardown()
            await own_db.close()

    def _create_local_orchestrator(
        self,
        budget_cap: int,
        parent_call_id: str | None = None,
    ) -> BaseOrchestrator | None:
        """Create the local orchestrator based on settings."""
        settings = get_settings()
        variant = settings.prioritizer_variant

        if variant == "experimental":
            orch = ExperimentalOrchestrator(
                self.db,
                self.broadcaster,
                budget_cap=budget_cap,
            )
            orch._parent_call_id = parent_call_id
            return orch
        if variant == "two_phase":
            orch = TwoPhaseOrchestrator(
                self.db,
                self.broadcaster,
                budget_cap=budget_cap,
            )
            orch._parent_call_id = parent_call_id
            return orch

        log.error("Unknown prioritizer_variant: %s", variant)
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

            local_done = self._local_task is None or self._local_task.done()
            if local_done and not turn_result.get("created_questions"):
                log.info("Global loop exiting: local done and no opportunity found")
                break

            if turn_result.get("dispatches"):
                dispatches = turn_result["dispatches"]
                sequences: list[list[Dispatch]] = []
                children: list[tuple[BaseOrchestrator, str]] = []

                dispatched_question_ids: set[str] = set()

                for d in dispatches:
                    if isinstance(d.payload, RecurseDispatchPayload):
                        resolved = await self.db.resolve_page_id(
                            d.payload.question_id,
                        )
                        if not resolved:
                            log.warning(
                                "Global recurse ID not found: %s",
                                d.payload.question_id[:8],
                            )
                            continue
                        child = self._create_local_orchestrator(
                            d.payload.budget,
                            parent_call_id=turn_result.get("call_id"),
                        )
                        if child is None:
                            continue
                        children.append((child, resolved))
                        dispatched_question_ids.add(resolved)
                        self._global_consumed += d.payload.budget
                        log.info(
                            "Global: queued recursive investigation: question=%s, budget=%d",
                            resolved[:8],
                            d.payload.budget,
                        )
                    else:
                        sequences.append([d])
                        dispatched_question_ids.add(
                            d.payload.question_id,
                        )

                tasks: list[asyncio.Task] = []  # type: ignore[type-arg]
                call_id = turn_result.get("call_id")
                trace: CallTrace | None = turn_result.get("trace")
                if sequences:
                    tasks.append(
                        asyncio.create_task(
                            self._run_sequences(
                                sequences,
                                root_question_id,
                                call_id,
                            ),
                            name="global_leaf_dispatches",
                        )
                    )
                    self._global_consumed += len(sequences)

                if children and trace:
                    child_qids = [qid for _, qid in children]
                    child_pages = await self.db.get_pages_by_ids(child_qids)
                    recurse_base = len(sequences)
                    for ci, (_child, child_qid) in enumerate(children):
                        child_page = child_pages.get(child_qid)
                        await trace.record(
                            DispatchExecutedEvent(
                                index=recurse_base + ci,
                                child_call_type="recurse",
                                question_id=child_qid,
                                question_headline=(child_page.headline if child_page else ""),
                            )
                        )

                for child, child_qid in children:
                    tasks.append(
                        asyncio.create_task(
                            child.run(child_qid),
                            name=f"global_recurse_{child_qid[:8]}",
                        )
                    )
                if tasks:
                    results = await asyncio.gather(
                        *tasks,
                        return_exceptions=True,
                    )
                    for r in results:
                        if isinstance(r, Exception):
                            log.error(
                                "Global dispatch task failed: %s",
                                r,
                                exc_info=r,
                            )

                dispatched_question_ids.discard(root_question_id)
                if dispatched_question_ids:
                    await self._assess_stale_questions(
                        dispatched_question_ids,
                        parent_call_id=turn_result.get("call_id"),
                    )

            researched_ids = turn_result.get("researched_question_ids", [])
            if researched_ids:
                reassessed = await self._propagate_updates(
                    root_question_id,
                    researched_ids,
                    parent_call_id=turn_result.get("call_id"),
                )
            else:
                reassessed = 0

            self._action_history.append(
                {
                    "created_questions": turn_result.get("created_questions", []),
                    "dispatches_count": len(turn_result.get("dispatches", [])),
                    "reassessed_count": reassessed,
                }
            )

            log.info(
                "Global turn complete: dispatches=%d, reassessed=%d, consumed=%d/%d",
                len(turn_result.get("dispatches", [])),
                reassessed,
                self._global_consumed,
                self._global_cap,
            )

    async def _wait_for_trigger(self) -> None:
        """Wait until enough new questions have been created or local task is done."""
        if self._local_task is None:
            log.info("Global trigger: no local task, proceeding immediately")
            return

        settings = get_settings()
        threshold = settings.global_prio_trigger_threshold

        while True:
            new_questions = await self.db.get_run_questions_since(
                self._last_trigger_at,
            )

            if len(new_questions) >= threshold:
                log.info(
                    "Global trigger: %d new questions (threshold=%d)",
                    len(new_questions),
                    threshold,
                )
                return

            if self._local_task.done():
                log.info("Global trigger: local task completed")
                return

            await asyncio.sleep(2)

    async def _global_turn(
        self,
        root_question_id: str,
    ) -> dict:
        """Run one global prioritisation turn (explore → decide → create).

        All three phases share the same system prompt, tool set, and
        growing message stack so the Anthropic API can cache the common
        prefix across phases.
        """
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

        # Single MoveState shared across phases: tools are bound to it once so
        # the tool list stays identical for prompt-cache stability.
        state = MoveState(p_call, self.db)
        system_prompt = build_system_prompt("global_prio")
        all_tools = await self._build_all_tools(
            root_question_id,
            trace,
            state,
        )

        try:
            await self._explore_phase(
                root_question_id,
                p_call,
                trace,
                state,
                system_prompt,
                all_tools,
            )
            await trace.record(
                GlobalPhaseCompletedEvent(
                    phase="explore",
                    outcome="Exploration complete",
                )
            )

            decision = await self._decide_phase(
                p_call,
                trace,
                state,
                system_prompt,
                all_tools,
            )
            await trace.record(
                GlobalPhaseCompletedEvent(
                    phase="decide",
                    outcome=(f"{'YES' if decision.should_create else 'NO'}: {decision.reasoning}"),
                )
            )

            if not decision.should_create:
                await mark_call_completed(
                    p_call,
                    self.db,
                    "No cross-cutting opportunity found",
                )
                self._turn_count += 1
                return {
                    "dispatches": [],
                    "call_id": p_call.id,
                    "trace": trace,
                    "created_questions": [],
                    "researched_question_ids": [],
                }

            create_result = await self._create_phase(
                p_call,
                trace,
                state,
                system_prompt,
                all_tools,
            )
            created_questions = create_result.get("created_questions", [])
            await trace.record(
                GlobalPhaseCompletedEvent(
                    phase="create",
                    outcome=f"{len(created_questions)} questions created",
                )
            )

            dispatches: list[Dispatch] = []
            if created_questions:
                dispatches = await self._dispatch_phase(
                    p_call,
                    trace,
                    state,
                    system_prompt,
                    all_tools,
                    created_questions,
                )
                await trace.record(
                    DispatchesPlannedEvent(
                        dispatches=[
                            DispatchTraceItem(
                                call_type=d.call_type.value,
                                **d.payload.model_dump(exclude_defaults=True),
                            )
                            for d in dispatches
                        ],
                    )
                )
                await trace.record(
                    GlobalPhaseCompletedEvent(
                        phase="dispatch",
                        outcome=f"{len(dispatches)} dispatches queued",
                    )
                )

            summary = (
                "Global turn complete. "
                f"{len(created_questions)} questions, "
                f"{len(dispatches)} dispatches."
            )
            await mark_call_completed(p_call, self.db, summary)
            self._turn_count += 1
            return {
                "dispatches": dispatches,
                "created_questions": created_questions,
                "researched_question_ids": [q["page_id"] for q in created_questions],
                "call_id": p_call.id,
                "trace": trace,
            }
        except Exception:
            log.exception("Global turn failed")
            await self.db.update_call_status(p_call.id, CallStatus.FAILED)
            await trace.record(ErrorEvent(message="Global turn failed"))
            self._turn_count += 1
            return {
                "dispatches": [],
                "call_id": p_call.id,
                "trace": trace,
                "created_questions": [],
                "researched_question_ids": [],
            }

    async def _build_all_tools(
        self,
        root_question_id: str,
        trace: CallTrace,
        state: MoveState,
    ) -> list[Tool]:
        """Build the full tool set shared across all three phases.

        Keeping tools identical across phases is critical for prompt
        caching — the Anthropic API caches the prefix (system + tools),
        so varying tools between phases would bust the cache.
        """
        global_impacts = await compute_global_impacts(
            root_question_id,
            self.db,
        )
        self._global_impacts = global_impacts

        explore_tools = [
            make_explore_subgraph_tool(
                self.db,
                trace,
                include_impact=True,
                global_impact=global_impacts or None,
                questions_only=True,
            ),
            make_load_page_tool(self.db, trace, default_detail="content"),
        ]

        create_tools: list[Tool] = []
        for mt in [MoveType.CREATE_QUESTION, MoveType.LINK_CHILD_QUESTION]:
            create_tools.append(MOVES[mt].bind(state))

        for ct in (CallType.FIND_CONSIDERATIONS, CallType.WEB_RESEARCH):
            ddef = DISPATCH_DEFS[ct]
            tool = ddef.bind(
                state,
                scope_question_id=root_question_id,
            )
            create_tools.append(tool)

        remaining_global = self._global_cap - self._global_consumed
        if remaining_global >= MIN_TWOPHASE_BUDGET:
            create_tools.append(
                RECURSE_DISPATCH_DEF.bind(
                    state,
                    scope_question_id=root_question_id,
                )
            )

        return explore_tools + create_tools

    async def _explore_phase(
        self,
        root_question_id: str,
        call: Call,
        trace: CallTrace,
        state: MoveState,
        system_prompt: str,
        tools: list[Tool],
    ) -> None:
        """Phase 1: agent loop exploring the research graph with tools."""
        settings = get_settings()
        global_impacts = getattr(self, "_global_impacts", None)

        if self._turn_count == 0:
            root_page = await self.db.get_page(root_question_id)
            root_detail = ""
            if root_page:
                root_detail = await format_page(
                    root_page,
                    PageDetail.CONTENT,
                    db=self.db,
                )

            subgraph = await render_question_subgraph(
                root_question_id,
                self.db,
                max_pages=settings.global_prio_subgraph_max_pages,
                include_impact=True,
                global_impact=global_impacts or None,
            )

            local_hint = await self._build_local_activity_hint()

            user_message = (
                "**You are in Phase 1: Explore.** Use only "
                "`explore_question_subgraph` and `load_page` tools. "
                "Do NOT use creation or dispatch tools in this phase.\n\n"
                "# Research Graph\n\n"
                "## Root Question\n\n"
                f"{root_detail}\n\n"
                "## Question Subgraph\n\n"
                f"{subgraph}\n\n"
                "## New Questions (from local prioritiser)\n\n"
                f"{local_hint}\n\n"
                "Explore the graph to identify cross-cutting research "
                "opportunities. End with a summary of what you found."
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
            self._messages.append(
                {
                    "role": "user",
                    "content": (
                        "**You are in Phase 1: Explore.** Use only "
                        "`explore_question_subgraph` and `load_page` tools. "
                        "Do NOT use creation or dispatch tools in this "
                        "phase.\n\n"
                        "## New Questions (from local prioritiser)\n\n"
                        f"{local_hint}\n\n"
                        "Continue exploring the graph for cross-cutting "
                        "opportunities. End with a summary of what you found."
                    ),
                }
            )

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
        system_prompt: str,
        tools: list[Tool],
    ) -> GlobalDecideResult:
        """Phase 2: decide whether a cross-cutting question is worth creating.

        Continues the explore message stack so the model has full context.
        Uses structured output (cache-compatible) to parse the decision.
        Tools are passed for cache stability but the model is told not to
        call any.
        """
        self._messages.append(
            {
                "role": "user",
                "content": (
                    "**You are now in Phase 2: Decide.** Do NOT call any "
                    "tools.\n\n"
                    "Based on your exploration above, are there cross-cutting "
                    "questions that, if answered, would substantially advance "
                    "2+ high-impact questions from different branches?"
                ),
            }
        )

        tool_defs, _ = prepare_tools(tools)
        meta = LLMExchangeMetadata(
            call_id=call.id,
            phase="global_decide",
        )
        result = await structured_call(
            system_prompt,
            response_model=GlobalDecideResult,
            messages=self._messages,
            tools=tool_defs,
            metadata=meta,
            db=self.db,
            cache=True,
        )

        if result.response_text:
            self._messages.append(
                {
                    "role": "assistant",
                    "content": result.response_text,
                }
            )

        decision = result.parsed or GlobalDecideResult(
            should_create=False,
            reasoning="Failed to parse decide response",
            question_headline=None,
            parent_question_ids=[],
        )
        log.info(
            "Global decide phase: should_create=%s, reasoning_len=%d",
            decision.should_create,
            len(decision.reasoning),
        )
        return decision

    async def _create_phase(
        self,
        call: Call,
        trace: CallTrace,
        state: MoveState,
        system_prompt: str,
        tools: list[Tool],
    ) -> dict:
        """Phase 3: create the cross-cutting question(s).

        Continues the message stack from decide so the model has full
        context from exploration and decision.
        """
        self._messages.append(
            {
                "role": "user",
                "content": (
                    "**You are now in Phase 3: Create.** Use "
                    "`create_question` to create the cross-cutting question "
                    "with links to the relevant parent questions."
                ),
            }
        )

        result = await run_single_call(
            system_prompt,
            tools=tools,
            call_id=call.id,
            phase="global_create",
            db=self.db,
            state=state,
            messages=self._messages,
            cache=True,
        )
        self._messages = result.messages

        page_ids = list(state.created_page_ids)
        pages_by_id = await self.db.get_pages_by_ids(page_ids)
        links_by_target = await self.db.get_links_to_many(page_ids)

        created_questions: list[dict] = []
        for pid in page_ids:
            page = pages_by_id.get(pid)
            if page:
                parent_links = [
                    l
                    for l in links_by_target.get(pid, [])
                    if l.link_type == LinkType.CHILD_QUESTION
                ]
                created_questions.append(
                    {
                        "headline": page.headline,
                        "parent_count": len(parent_links),
                        "page_id": pid,
                    }
                )

        return {"created_questions": created_questions}

    async def _dispatch_phase(
        self,
        call: Call,
        trace: CallTrace,
        state: MoveState,
        system_prompt: str,
        tools: list[Tool],
        created_questions: list[dict],
    ) -> list[Dispatch]:
        """Phase 4: dispatch research on each newly created question.

        Runs one LLM call per created question, concurrently. Each call
        sees the question headline and is asked to choose a dispatch
        strategy.
        """
        remaining_global = self._global_cap - self._global_consumed

        async def dispatch_one(question: dict) -> None:
            qid = question["page_id"]
            headline = question["headline"]
            dispatch_state = MoveState(call, self.db)

            user_msg = (
                "**You are now in Phase 4: Dispatch.** "
                "Dispatch research on this newly created cross-cutting "
                "question:\n\n"
                f"**{qid[:8]}**: {headline}\n\n"
                f"You have **{remaining_global} budget units** remaining "
                "for the entire global prioritiser (this dispatch + future "
                "turns + propagation). Do not allocate more than half to "
                "this dispatch. See the Phase 4 guidance in your system "
                "prompt for budget allocation rules."
            )

            await run_single_call(
                system_prompt,
                user_message=user_msg,
                tools=tools,
                call_id=call.id,
                phase="global_dispatch",
                db=self.db,
                state=dispatch_state,
                cache=True,
            )
            state.dispatches.extend(dispatch_state.dispatches)

        await asyncio.gather(*(dispatch_one(q) for q in created_questions))

        return list(state.dispatches)

    async def _build_local_activity_hint(self) -> str:
        """Summarise questions created since the last global turn.

        Shows each new question with its parent question(s) so the
        explore phase can see where new branches have appeared.
        Updates ``_last_trigger_at`` so the next call only sees
        questions created after this point.
        """
        try:
            new_questions = await self.db.get_run_questions_since(
                self._last_trigger_at,
            )
            self._last_trigger_at = datetime.now(UTC)

            if not new_questions:
                return "(no new questions since last turn)"

            new_ids = [q.id for q in new_questions]
            links_by_child = await self.db.get_links_to_many(new_ids)

            parent_ids: set[str] = set()
            for links in links_by_child.values():
                for link in links:
                    if link.link_type == LinkType.CHILD_QUESTION:
                        parent_ids.add(link.from_page_id)
            parent_pages = await self.db.get_pages_by_ids(list(parent_ids))

            lines: list[str] = [
                f"{len(new_questions)} new questions since last turn:",
            ]
            for q in new_questions:
                parent_links = [
                    l
                    for l in links_by_child.get(q.id, [])
                    if l.link_type == LinkType.CHILD_QUESTION
                ]
                if parent_links:
                    parent_labels = []
                    for pl in parent_links:
                        pp = parent_pages.get(pl.from_page_id)
                        label = (
                            f'"{pp.headline[:50]}" [{pl.from_page_id[:8]}]'
                            if pp
                            else f"[{pl.from_page_id[:8]}]"
                        )
                        parent_labels.append(label)
                    parents_str = ", ".join(parent_labels)
                    lines.append(f'- [{q.id[:8]}] "{q.headline}" (child of {parents_str})')
                else:
                    lines.append(f'- [{q.id[:8]}] "{q.headline}" (root-level)')
            return "\n".join(lines)
        except Exception:
            log.debug("Could not build activity hint", exc_info=True)
            return "(no activity data available)"

    async def _assess_stale_questions(
        self,
        question_ids: set[str],
        parent_call_id: str | None = None,
    ) -> int:
        """Assess dispatched questions that have become stale.

        Checks each question via ``get_assess_staleness`` and only runs
        an assess call when new evidence has arrived since the last
        completed assess. Returns the number of assess calls made.
        """
        staleness = await self.db.get_assess_staleness(list(question_ids))
        stale_ids = [qid for qid, is_stale in staleness.items() if is_stale]
        if not stale_ids:
            return 0

        assessed = 0
        for qid in stale_ids:
            if self._global_consumed >= self._global_cap:
                break
            try:
                call_id = await assess_question(
                    qid,
                    self.db,
                    parent_call_id=parent_call_id,
                    broadcaster=self.broadcaster,
                    force=True,
                    summarise=self.summarise_before_assess,
                )
                if call_id:
                    self._global_consumed += 1
                    assessed += 1
                    log.info(
                        "Global post-dispatch assess: question=%s",
                        qid[:8],
                    )
            except Exception:
                log.warning(
                    "Global post-dispatch assess failed: %s",
                    qid[:8],
                    exc_info=True,
                )
        return assessed

    async def _propagate_updates(
        self,
        root_question_id: str,
        researched_question_ids: Sequence[str],
        parent_call_id: str | None = None,
    ) -> int:
        """Re-assess along highest-impact paths from researched questions to root.

        Uses a multiplicative impact model: the impact of a path is the
        product of edge impacts (each normalised to 0-1). We transform to
        additive weights via d(e) = -log(impact/10) so highest-impact paths
        become shortest paths, then use Yen's K-shortest-paths to find
        candidates.

        Greedily commits to paths: the best path is always taken in full,
        then additional paths are taken while budget permits. A partial
        final path is allowed.

        Returns the number of re-assess calls made.
        """
        graph = await self._build_propagation_graph(
            root_question_id,
            researched_question_ids,
        )
        if not graph:
            return 0

        paths = _find_propagation_paths(
            graph,
            researched_question_ids,
            root_question_id,
            k_per_source=3,
        )
        if not paths:
            return 0

        remaining_budget = self._global_cap - self._global_consumed
        propagation_budget = min(remaining_budget, MAX_PROPAGATION_REASSESS)
        ordered_nodes = _select_nodes_from_paths(paths, budget=propagation_budget)
        if not ordered_nodes:
            return 0

        reassessed = 0
        for qid in ordered_nodes:
            if self._global_consumed >= self._global_cap:
                break
            try:
                call_id = await assess_question(
                    qid,
                    self.db,
                    parent_call_id=parent_call_id,
                    broadcaster=self.broadcaster,
                    force=True,
                    summarise=self.summarise_before_assess,
                )
                if call_id:
                    self._global_consumed += 1
                    reassessed += 1
                    log.info(
                        "Global propagation: re-assessed question %s",
                        qid[:8],
                    )
            except Exception:
                log.warning(
                    "Global propagation: failed to re-assess %s",
                    qid[:8],
                    exc_info=True,
                )

        return reassessed

    async def _build_propagation_graph(
        self,
        root_question_id: str,
        researched_question_ids: Sequence[str],
    ) -> dict[str, dict[str, float]]:
        """BFS upward from researched questions to root via CHILD_QUESTION links.

        Returns an adjacency dict: ``graph[child][parent] = weight`` where
        weight = -log(impact/10). O(depth) DB round trips.
        """
        graph: dict[str, dict[str, float]] = {}
        visited: set[str] = set(researched_question_ids)
        frontier = list(researched_question_ids)

        for _ in range(20):
            if not frontier:
                break

            incoming = await self.db.get_links_to_many(frontier)

            next_frontier: list[str] = []
            for child_id in frontier:
                for link in incoming.get(child_id, []):
                    if link.link_type != LinkType.CHILD_QUESTION:
                        continue
                    parent_id = link.from_page_id
                    impact = link.impact_on_parent_question or 5
                    weight = -math.log(max(impact, 1) / 10.0)

                    if child_id not in graph:
                        graph[child_id] = {}
                    graph[child_id][parent_id] = weight

                    if parent_id not in visited:
                        visited.add(parent_id)
                        if parent_id != root_question_id:
                            next_frontier.append(parent_id)

            frontier = next_frontier

        return graph
