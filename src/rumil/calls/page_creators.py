"""WorkspaceUpdater implementations for all call types."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

import anthropic
from anthropic.types import ServerToolUseBlock, ToolUseBlock
from pydantic import BaseModel, Field

from rumil.budget import _consume_budget
from rumil.calls.common import (
    execute_tool_uses,
    prepare_tools,
    record_round_moves,
    run_agent_loop,
)
from rumil.calls.stages import CallInfra, ContextResult, UpdateResult, WorkspaceUpdater
from rumil.llm import (
    LLMExchangeMetadata,
    Tool,
    build_system_prompt,
    build_user_message,
    call_api,
    structured_call,
)
from rumil.models import (
    CallType,
    MoveType,
)
from rumil.moves.create_claim import (
    execute_with_source_creation,
)
from rumil.moves.load_page import LoadPagePayload
from rumil.moves.registry import MOVES
from rumil.settings import get_settings

log = logging.getLogger(__name__)


class SimpleAgentLoop(WorkspaceUpdater):
    """Single-pass agent loop. Used by most call types."""

    def __init__(
        self,
        call_type: CallType,
        task_description: str,
        available_moves: Sequence[MoveType] | None = None,
        max_rounds: int | None = None,
        prompt_name: str | None = None,
    ) -> None:
        self._call_type = call_type
        self._task_description = task_description
        self._available_moves = available_moves
        self._max_rounds = max_rounds
        self._prompt_name = prompt_name

    async def update_workspace(
        self,
        infra: CallInfra,
        context: ContextResult,
    ) -> UpdateResult:
        settings = get_settings()
        max_rounds = self._max_rounds
        if max_rounds is None:
            max_rounds = 1 if settings.is_smoke_test else 3

        infra.state.context_page_ids = set(context.working_page_ids) | set(context.preloaded_ids)
        moves_list = (
            list(self._available_moves) if self._available_moves is not None else list(MoveType)
        )
        tools = [MOVES[mt].bind(infra.state) for mt in moves_list]
        system_prompt = build_system_prompt(self._prompt_name or self._call_type.value)
        user_message = build_user_message(
            context.context_text,
            self._task_description,
        )

        agent_result = await run_agent_loop(
            system_prompt,
            user_message,
            tools,
            call_id=infra.call.id,
            db=infra.db,
            state=infra.state,
            max_rounds=max_rounds,
            cache=True,
        )

        log.info(
            "update_workspace complete: type=%s, pages_created=%d, dispatches=%d, moves=%d",
            self._call_type.value,
            len(infra.state.created_page_ids),
            len(infra.state.dispatches),
            len(infra.state.moves),
        )

        loaded_raw: list[str] = []
        for move in infra.state.moves:
            if move.move_type is MoveType.LOAD_PAGE and isinstance(move.payload, LoadPagePayload):
                loaded_raw.append(move.payload.page_id)
        resolved_loaded: list[str] = []
        for raw in loaded_raw:
            full = await infra.db.resolve_page_id(raw)
            if full:
                resolved_loaded.append(full)
        all_loaded_ids = list(dict.fromkeys([*context.preloaded_ids, *resolved_loaded]))

        return UpdateResult(
            created_page_ids=infra.state.created_page_ids,
            moves=infra.state.moves,
            all_loaded_ids=all_loaded_ids,
            dispatches=infra.state.dispatches,
            messages=agent_result.messages,
        )


_CONTINUE_TEMPLATE = (
    "Continue this task. You have already made contributions in prior rounds "
    "(visible above). Focus on NEW angles, evidence, or sub-questions you "
    "have not yet covered — prioritise those that would most add to the "
    "eventual judgement on this question.\n\n"
    "Question ID: `{question_id}`"
)

_FRUIT_CHECK_MESSAGE = (
    "Before continuing, rate how much useful scouting work remains on this "
    "scope question. Consider what you have already contributed and what "
    "angles are left unexplored. Respond with remaining_fruit (0-10) and "
    "brief_reasoning. Do not call any tools — they will have no effect here."
)


class _FruitCheck(BaseModel):
    remaining_fruit: int = Field(
        description=(
            "0-10 integer: how much useful work remains on this scope. "
            "0 = nothing more to add; 1-2 = close to exhausted; "
            "3-4 = most angles covered; 5-6 = diminishing but real returns; "
            "7-8 = substantial work remains; 9-10 = barely started"
        )
    )
    brief_reasoning: str = Field(description="One sentence explaining why you chose this score")


class MultiRoundLoop(WorkspaceUpdater):
    """Multi-round loop with fruit checking and conversation resumption."""

    def __init__(
        self,
        max_rounds: int,
        fruit_threshold: int,
        available_moves: Sequence[MoveType] | None = None,
        call_type: CallType = CallType.FIND_CONSIDERATIONS,
        task_description: str | None = None,
    ) -> None:
        self._max_rounds = max_rounds
        self._fruit_threshold = fruit_threshold
        self._available_moves = available_moves
        self._call_type = call_type
        self._task_description = task_description

    async def update_workspace(
        self,
        infra: CallInfra,
        context: ContextResult,
    ) -> UpdateResult:
        infra.state.context_page_ids = set(context.working_page_ids) | set(context.preloaded_ids)
        moves_list = (
            list(self._available_moves) if self._available_moves is not None else list(MoveType)
        )
        tools = [MOVES[mt].bind(infra.state) for mt in moves_list]
        tool_defs, _ = prepare_tools(tools)
        system_prompt = build_system_prompt(self._call_type.value)

        if self._task_description is not None:
            task = self._task_description
        else:
            task = (
                "Generate considerations that would most improve the next "
                "judgement on this question.\n\n"
                "Question ID (use this when linking considerations): "
                f"`{infra.question_id}`"
            )
        user_message = build_user_message(context.context_text, task)

        resume_messages: list[dict] = []
        rounds_completed = 0
        last_fruit_score: int | None = None

        for i in range(self._max_rounds):
            if not await _consume_budget(infra.db, pool_question_id=infra.pool_question_id):
                log.info(
                    "Budget exhausted, stopping scout session at round %d",
                    i,
                )
                break

            if i == 0:
                agent_result = await run_agent_loop(
                    system_prompt,
                    user_message=user_message,
                    tools=tools,
                    call_id=infra.call.id,
                    db=infra.db,
                    state=infra.state,
                    cache=True,
                )
            else:
                continue_msg = _CONTINUE_TEMPLATE.format(
                    question_id=infra.question_id,
                )
                resume_messages.append({"role": "user", "content": continue_msg})
                agent_result = await run_agent_loop(
                    system_prompt,
                    tools=tools,
                    call_id=infra.call.id,
                    db=infra.db,
                    state=infra.state,
                    messages=resume_messages,
                    cache=True,
                )

            rounds_completed += 1
            resume_messages = list(agent_result.messages)

            if i >= self._max_rounds - 1:
                break
            last_fruit_score = await self._run_fruit_check(
                infra,
                system_prompt,
                resume_messages,
                tool_defs,
                _FruitCheck,
            )
            if last_fruit_score <= self._fruit_threshold:
                log.info(
                    "Scout fruit (%d) <= threshold (%d), stopping after round %d",
                    last_fruit_score,
                    self._fruit_threshold,
                    i + 1,
                )
                break

        return UpdateResult(
            created_page_ids=infra.state.created_page_ids,
            moves=infra.state.moves,
            all_loaded_ids=[],
            dispatches=infra.state.dispatches,
            messages=resume_messages,
            last_fruit_score=last_fruit_score,
            rounds_completed=rounds_completed,
        )

    async def _run_fruit_check(
        self,
        infra: CallInfra,
        system_prompt: str,
        resume_messages: list[dict],
        tool_defs: list[dict],
        fruit_check_model: type[_FruitCheck],
    ) -> int:
        check_messages = [*list(resume_messages), {"role": "user", "content": _FRUIT_CHECK_MESSAGE}]
        meta = LLMExchangeMetadata(
            call_id=infra.call.id,
            phase="fruit_check",
            user_message=_FRUIT_CHECK_MESSAGE,
        )
        result = await structured_call(
            system_prompt=system_prompt,
            response_model=fruit_check_model,
            messages=check_messages,
            tools=tool_defs,
            metadata=meta,
            db=infra.db,
            cache=True,
        )
        if result.parsed:
            log.info(
                "Fruit check: score=%d, reasoning=%s",
                result.parsed.remaining_fruit,
                result.parsed.brief_reasoning,
            )
            return result.parsed.remaining_fruit
        log.warning("Fruit check returned empty data, defaulting to 5")
        return 5


class WebResearchLoop(WorkspaceUpdater):
    """Multi-round web research loop with server tools."""

    def __init__(
        self,
        allowed_domains: Sequence[str] | None = None,
        available_moves: Sequence[MoveType] | None = None,
    ) -> None:
        self._allowed_domains = allowed_domains
        self._available_moves = available_moves
        self.source_page_ids: dict[str, str] = {}

    async def update_workspace(
        self,
        infra: CallInfra,
        context: ContextResult,
    ) -> UpdateResult:
        settings = get_settings()
        max_rounds = 2 if settings.is_smoke_test else 5
        client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())

        infra.state.context_page_ids = set(context.working_page_ids) | set(context.preloaded_ids)
        server_tools = self._build_server_tools()
        moves_list = (
            list(self._available_moves)
            if self._available_moves is not None
            else list(WEB_RESEARCH_MOVES)
        )
        custom_tools = [MOVES[mt].bind(infra.state) for mt in moves_list]
        custom_tools = self._wrap_create_claim(custom_tools, infra)
        custom_tool_defs, custom_tool_fns = prepare_tools(custom_tools)
        all_tool_defs: list = server_tools + custom_tool_defs

        system_prompt = build_system_prompt("web_research")
        task = (
            "Search the web for evidence relevant to this question and create "
            "source-grounded claims.\n\n"
            "Question ID (use this when linking considerations): "
            f"`{infra.question_id}`"
        )
        user_message = build_user_message(context.context_text, task)
        messages: list[dict] = [{"role": "user", "content": user_message}]

        log.debug(
            "Web research update_workspace starting: "
            "system_prompt=%d chars, user_message=%d chars, "
            "server_tools=%d, custom_tools=%d, all_tool_defs=%d",
            len(system_prompt),
            len(user_message),
            len(server_tools),
            len(custom_tool_defs),
            len(all_tool_defs),
        )
        tool_defs_chars = len(json.dumps(all_tool_defs))
        log.debug(
            "Tool definitions total: %d chars (%d tokens approx)",
            tool_defs_chars,
            tool_defs_chars // 4,
        )

        for round_num in range(max_rounds):
            total_msg_chars = sum(len(str(m.get("content", ""))) for m in messages)
            log.debug(
                "Round %d: %d messages, ~%d chars in messages",
                round_num,
                len(messages),
                total_msg_chars,
            )
            meta = LLMExchangeMetadata(
                call_id=infra.call.id,
                phase="web_research_loop",
                round_num=round_num,
                user_message=user_message if round_num == 0 else None,
            )
            api_resp = await call_api(
                client,
                settings.model,
                system_prompt,
                messages,
                all_tool_defs,
                metadata=meta,
                db=infra.db,
                cache=True,
            )
            response = api_resp.message

            custom_tool_uses: list[ToolUseBlock] = []
            for block in response.content:
                if isinstance(block, ToolUseBlock):
                    custom_tool_uses.append(block)

            messages.append({"role": "assistant", "content": response.content})

            if custom_tool_uses:
                _, tool_results = await execute_tool_uses(
                    custom_tool_uses,
                    custom_tool_fns,
                )
                await record_round_moves(
                    state=infra.state,
                    db=infra.db,
                )
                messages.append({"role": "user", "content": tool_results})

            if response.stop_reason == "end_turn" or not (
                custom_tool_uses or any(isinstance(b, ServerToolUseBlock) for b in response.content)
            ):
                break

        log.info(
            "Web research update_workspace complete: %d pages created, %d sources",
            len(infra.state.created_page_ids),
            len(self.source_page_ids),
        )

        return UpdateResult(
            created_page_ids=infra.state.created_page_ids,
            moves=infra.state.moves,
            all_loaded_ids=[],
            dispatches=infra.state.dispatches,
            messages=messages,
        )

    def _build_server_tools(self) -> list[dict]:
        web_search: dict = {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5,
        }
        if self._allowed_domains:
            web_search["allowed_domains"] = list(self._allowed_domains)

        return [web_search]

    def _wrap_create_claim(
        self,
        tools: list[Tool],
        infra: CallInfra,
    ) -> list[Tool]:
        wrapped: list[Tool] = []
        for tool in tools:
            if tool.name == "create_claim":

                async def wrapped_fn(
                    inp: dict,
                    _infra=infra,
                ) -> str:
                    known_before = set(self.source_page_ids.values())
                    result = await execute_with_source_creation(
                        inp, _infra.call, _infra.db, self.source_page_ids
                    )
                    for sid in self.source_page_ids.values():
                        if sid not in known_before:
                            _infra.state.created_page_ids.append(sid)
                    if result.created_page_id:
                        _infra.state.created_page_ids.append(result.created_page_id)
                    return result.message

                wrapped.append(
                    Tool(
                        name=tool.name,
                        description=tool.description,
                        input_schema=tool.input_schema,
                        fn=wrapped_fn,
                    )
                )
            else:
                wrapped.append(tool)
        return wrapped


WEB_RESEARCH_MOVES = [
    MoveType.CREATE_CLAIM,
    MoveType.LINK_CONSIDERATION,
    MoveType.LOAD_PAGE,
]
