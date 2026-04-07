"""Runner for the scope-subquestion linker agent."""

import json
import logging
import re

from rumil.calls.common import run_agent_loop
from rumil.database import DB
from rumil.models import Call, CallStatus, CallType, PageType
from rumil.moves.base import MoveState
from rumil.scope_subquestion_linker.prompt import build_linker_prompt
from rumil.scope_subquestion_linker.seed_selection import select_seed_questions
from rumil.scope_subquestion_linker.subgraph import render_question_subgraph
from rumil.scope_subquestion_linker.tool import make_render_subgraph_tool
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import LinkSubquestionsCompleteEvent
from rumil.tracing.tracer import CallTrace, set_trace

log = logging.getLogger(__name__)


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


async def run_scope_subquestion_linker(
    scope_question_id: str,
    db: DB,
    *,
    max_rounds: int | None = None,
    broadcaster: Broadcaster | None = None,
) -> Call:
    """Explore the workspace looking for subquestions to link to a scope question.

    Returns the persisted Call. The call's `review_json` will contain
    `{"proposed_subquestion_ids": [...], "rationales": {...}}`. The agent does
    NOT create LINK_CHILD_QUESTION links itself; that is left to a follow-up
    review step.
    """
    settings = get_settings()

    resolved_id = await db.resolve_page_id(scope_question_id)
    if resolved_id is None:
        raise ValueError(f'Scope question "{scope_question_id}" not found')
    scope = await db.get_page(resolved_id)
    if scope is None:
        raise ValueError(f'Scope question "{resolved_id}" not found')
    if scope.page_type != PageType.QUESTION:
        raise ValueError(
            f"Page `{resolved_id[:8]}` is not a question (type={scope.page_type.value})"
        )

    call = await db.create_call(
        call_type=CallType.LINK_SUBQUESTIONS,
        scope_page_id=resolved_id,
    )
    trace = CallTrace(call.id, db, broadcaster=broadcaster)
    set_trace(trace)
    await db.update_call_status(call.id, CallStatus.RUNNING)

    effective_max_rounds = max_rounds or settings.scope_subquestion_linker_max_rounds

    try:
        seeds = await select_seed_questions(
            scope, db, limit=settings.scope_subquestion_linker_seed_limit
        )
        seed_blocks: list[str] = []
        for seed in seeds:
            sub = await render_question_subgraph(seed.id, db)
            seed_blocks.append(sub)
        seed_block = "\n\n".join(seed_blocks)

        current_children = await db.get_child_questions(scope.id)
        current_children_ids: set[str] = {c.id for c in current_children}
        if current_children:
            current_children_block = "\n".join(
                f"- `{c.id[:8]}` -- {c.headline}" for c in current_children
            )
        else:
            current_children_block = ""

        system_prompt = build_linker_prompt(
            scope, current_children_block, seed_block, effective_max_rounds
        )
        user_message = (
            f"Find subquestions to link to scope `{scope.id[:8]}`: {scope.headline}"
        )

        state = MoveState(call, db)
        tools = [make_render_subgraph_tool(db, trace)]

        agent_result = await run_agent_loop(
            system_prompt,
            user_message,
            tools,
            call_id=call.id,
            db=db,
            state=state,
            max_rounds=effective_max_rounds,
            cache=True,
        )

        proposed_ids, rationales = await _extract_and_validate(
            agent_result.text, db, scope.id, current_children_ids
        )

        await trace.record(
            LinkSubquestionsCompleteEvent(
                proposed_ids=proposed_ids, rationales=rationales
            )
        )

        call.review_json = {
            "proposed_subquestion_ids": proposed_ids,
            "rationales": rationales,
        }
        call.result_summary = f"{len(proposed_ids)} proposed subquestion(s)"
        call.status = CallStatus.COMPLETE
        await db.save_call(call)
    except Exception:
        log.exception("Scope subquestion linker failed")
        await db.update_call_status(call.id, CallStatus.FAILED)
        raise

    return call


async def _extract_and_validate(
    agent_text: str,
    db: DB,
    scope_id: str,
    current_children_ids: set[str],
) -> tuple[list[str], dict[str, str]]:
    """Extract the JSON block from the agent's final text and validate every id."""
    raw = _extract_json_block(agent_text)
    if raw is None:
        log.warning("scope_subquestion_linker: no JSON block in final agent text")
        return [], {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("scope_subquestion_linker: JSON parse failed: %s", exc)
        return [], {}

    raw_ids = data.get("linked_question_ids") or []
    raw_rationales = data.get("rationales") or {}
    if not isinstance(raw_ids, list) or not isinstance(raw_rationales, dict):
        log.warning("scope_subquestion_linker: malformed JSON shape")
        return [], {}

    proposed_ids: list[str] = []
    rationales: dict[str, str] = {}
    seen: set[str] = set()
    for entry in raw_ids:
        if not isinstance(entry, str):
            continue
        rationale = raw_rationales.get(entry) or raw_rationales.get(entry.strip())
        if not isinstance(rationale, str) or not rationale.strip():
            log.info("dropping %s: missing rationale", entry)
            continue
        resolved = await db.resolve_page_id(entry.strip())
        if resolved is None:
            log.info("dropping %s: not found", entry)
            continue
        if resolved == scope_id:
            log.info("dropping %s: is scope itself", entry)
            continue
        if resolved in current_children_ids:
            log.info("dropping %s: already a child of scope", entry)
            continue
        if resolved in seen:
            continue
        page = await db.get_page(resolved)
        if page is None or page.page_type != PageType.QUESTION:
            log.info("dropping %s: not a question", entry)
            continue
        proposed_ids.append(resolved)
        rationales[resolved] = rationale.strip()
        seen.add(resolved)

    return proposed_ids, rationales


def _extract_json_block(text: str) -> str | None:
    """Find the last fenced JSON block in *text* and return its inner string."""
    matches = _JSON_BLOCK_RE.findall(text or "")
    if matches:
        return matches[-1]
    # Fallback: try to find a bare {...} that contains "linked_question_ids"
    idx = (text or "").find('"linked_question_ids"')
    if idx == -1:
        return None
    start = (text or "").rfind("{", 0, idx)
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None
