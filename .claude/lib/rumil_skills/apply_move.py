"""Apply one rumil move from Claude Code context onto the chat envelope.

This is the *cc-mediated* lane: Claude Code is the brain, deciding from
its broader conversation context that a specific move should happen.
The move is applied directly (no rumil-internal LLM call) and is owned
by the CLAUDE_CODE_DIRECT envelope Call. Its presence in the trace — and
the envelope's call_type — make the provenance unambiguous.

By contrast, /rumil-dispatch fires a *full* rumil call where the rumil
prompt and tools decide what moves to make.

Usage:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.apply_move \\
        <move_type> '<payload_json>'

    # Example: add a subquestion under parent Q#abc12345
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.apply_move \\
        CREATE_QUESTION '{"headline": "What are X's second-order effects?",
                          "content": "Explore downstream..."}'

    # Then link it as a child
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.apply_move \\
        LINK_CHILD_QUESTION '{"parent_id": "abc12345", "child_id": "def67890"}'

    # Dry run — validate payload, show what would happen, don't execute
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.apply_move \\
        CREATE_QUESTION '{...}' --dry-run

    # Restrict to accreting moves only (additions, flags, reports)
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.apply_move \\
        CREATE_QUESTION '{...}' --accreting-only

    # List available moves
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.apply_move --list

    # Show full payload schema for one move (fields, types, required/optional,
    # descriptions, nested models). Use when --list isn't enough.
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.apply_move \\
        --schema CREATE_SUBQUESTION
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import types
import typing
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from rumil.database import DB
from rumil.models import Call, MoveType
from rumil.moves.base import MoveResult
from rumil.moves.registry import MOVES
from rumil.tracing.trace_events import MoveTraceItem, MovesExecutedEvent, PageRef

from ._format import print_event, print_trace, truncate
from ._runctx import ensure_chat_envelope


class TraceRecordError(RuntimeError):
    """Raised when a move landed in the DB but its trace event could not be recorded.

    The caller should treat this as a hard failure: the envelope call now looks
    empty (or incomplete) in the frontend even though the mutation is live in
    the workspace.
    """


# Allowlist of moves that only *add* to the workspace. Keeps rumil-clean
# and other cc-mediated skills safe-by-default: no destructive or in-place edits.
ACCRETING_MOVES: frozenset[MoveType] = frozenset(
    {
        MoveType.CREATE_CLAIM,
        MoveType.CREATE_QUESTION,
        MoveType.CREATE_SCOUT_QUESTION,
        MoveType.CREATE_SUBQUESTION,
        MoveType.CREATE_JUDGEMENT,
        MoveType.CREATE_WIKI_PAGE,
        MoveType.CREATE_VIEW_ITEM,
        MoveType.PROPOSE_VIEW_ITEM,
        MoveType.LINK_CONSIDERATION,
        MoveType.LINK_CHILD_QUESTION,
        MoveType.LINK_RELATED,
        MoveType.LINK_VARIANT,
        MoveType.LINK_DEPENDS_ON,
        MoveType.FLAG_FUNNINESS,
        MoveType.REPORT_DUPLICATE,
        MoveType.LOAD_PAGE,  # read-only, harmless
    }
)


def _list_moves() -> None:
    print("Available moves:")
    print("  (A = accreting — safe for --accreting-only; D = destructive/in-place)")
    for mt in MoveType:
        move_def = MOVES.get(mt)
        if move_def is None:
            continue
        marker = "A" if mt in ACCRETING_MOVES else "D"
        desc = " ".join((move_def.description or "").split())[:160]
        print(f"  [{marker}] {mt.value:<24} {desc}")
    print()
    print("  For full field details on one move, use --schema <MOVE_TYPE>.")


def _format_type(annotation: object) -> str:
    """Render a type annotation compactly, stripping module prefixes and
    Annotated metadata so the LLM can read it at a glance.
    """
    if annotation is type(None):
        return "None"
    if isinstance(annotation, type):
        return annotation.__name__
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    # Annotated[T, ...metadata] — keep T, drop metadata.
    if "Annotated" in repr(annotation).split("[", 1)[0] and args:
        return _format_type(args[0])
    # Union / X | None
    if origin is typing.Union or origin is types.UnionType:
        return " | ".join(_format_type(a) for a in args)
    # Parameterised generic (list[...], dict[...], Literal[...], ...)
    if origin is not None:
        origin_name = (
            getattr(origin, "__name__", None)
            or getattr(origin, "_name", None)
            or str(origin)
        )
        if args:
            return f"{origin_name}[{', '.join(_format_type(a) for a in args)}]"
        return str(origin_name)
    return str(annotation)


def _render_field_lines(schema_cls: type[BaseModel], indent: str) -> list[str]:
    lines: list[str] = []
    for name, info in schema_cls.model_fields.items():
        type_str = _format_type(info.annotation)
        marker = "required" if info.is_required() else "optional"
        default_str = ""
        if not info.is_required():
            if info.default_factory is not None:
                try:
                    default_str = f" = {info.default_factory()!r}"
                except Exception:
                    default_str = " = <factory>"
            else:
                default_str = f" = {info.default!r}"
        lines.append(f"{indent}{name}: {type_str} [{marker}]{default_str}")
        if info.description:
            for dl in info.description.strip().splitlines():
                lines.append(f"{indent}    {dl.strip()}")
    return lines


def _collect_nested_models(
    annotation: object,
    seen: set[type],
) -> list[type[BaseModel]]:
    found: list[type[BaseModel]] = []
    origin = typing.get_origin(annotation)
    if origin is None:
        if (
            isinstance(annotation, type)
            and issubclass(annotation, BaseModel)
            and annotation not in seen
        ):
            seen.add(annotation)
            found.append(annotation)
        return found
    for arg in typing.get_args(annotation):
        found.extend(_collect_nested_models(arg, seen))
    return found


def _render_schema(move_type: MoveType) -> str:
    move_def = MOVES.get(move_type)
    if move_def is None:
        return f"(no MoveDef registered for {move_type.value})"
    schema_cls = move_def.schema
    lines: list[str] = []
    lines.append(f"{move_type.value}  ({schema_cls.__name__})")
    if move_def.description:
        desc = " ".join(move_def.description.split())
        lines.append("")
        lines.append(f"  {desc}")
    lines.append("")
    lines.append("  Fields:")
    lines.extend(_render_field_lines(schema_cls, indent="    "))

    seen: set[type] = {schema_cls}
    nested: list[type[BaseModel]] = []
    for _, info in schema_cls.model_fields.items():
        nested.extend(_collect_nested_models(info.annotation, seen))
    if nested:
        lines.append("")
        lines.append("  Nested models:")
        for nested_cls in nested:
            lines.append(f"    {nested_cls.__name__}:")
            lines.extend(_render_field_lines(nested_cls, indent="      "))
    return "\n".join(lines)


def _payload_preview(payload_dict: dict) -> str:
    """Compact human-readable preview of a move payload for dry-run."""
    preview_parts: list[str] = []
    for k, v in payload_dict.items():
        if isinstance(v, str):
            shown = truncate(v, 80)
        elif isinstance(v, (list, dict)):
            shown = truncate(json.dumps(v, default=str), 80)
        else:
            shown = str(v)
        preview_parts.append(f"    {k}: {shown}")
    return "\n".join(preview_parts) or "    (empty payload)"


_HEADLINE_FIELDS = ("headline", "note", "reasoning", "content", "text", "issue")


def _extract_trace_headline(validated_payload, fallback_message: str) -> str:
    """Pick a useful short label for a move trace item.

    CREATE_* moves have ``headline``. FLAG/REPORT moves have ``note``.
    LINK moves have ``reasoning``. Fall back to ``result.message`` last
    (which is often "Done." and not informative, so it's a last resort).
    """
    for field in _HEADLINE_FIELDS:
        v = getattr(validated_payload, field, None)
        if isinstance(v, str) and v.strip():
            return truncate(v.strip(), 100)
    if fallback_message.strip() and fallback_message.strip() != "Done.":
        return truncate(fallback_message.strip(), 100)
    return ""


async def _page_refs_with_headlines(db, created_page_ids: list[str]) -> list[PageRef]:
    """Build PageRefs with headlines filled in from the DB (one batch query)."""
    if not created_page_ids:
        return []
    pages = await db.get_pages_by_ids(created_page_ids)
    refs: list[PageRef] = []
    for pid in created_page_ids:
        page = pages.get(pid) if isinstance(pages, dict) else None
        headline = getattr(page, "headline", "") if page else ""
        refs.append(PageRef(id=pid, headline=truncate(headline, 100)))
    return refs


async def _record_envelope_trace_event(
    db: DB,
    envelope_call_id: str,
    move_type: MoveType,
    validated_payload: BaseModel,
    created_page_ids: list[str],
    message: str,
) -> None:
    """Append a MovesExecutedEvent to the envelope call's trace.

    Without this, cc-mediated mutations hit the DB but don't show up
    in the rumil frontend's trace view — making the envelope call look
    empty even after many moves have been applied. The caller must treat
    a raised ``TraceRecordError`` as a loud failure: the move has already
    landed, but the envelope is now out of sync with reality.
    """
    headline = _extract_trace_headline(validated_payload, message)
    page_refs = await _page_refs_with_headlines(db, created_page_ids)
    trace_item = MoveTraceItem(
        type=move_type.value,
        headline=headline,
        page_refs=page_refs,
    )
    event = MovesExecutedEvent(moves=[trace_item])
    dumped = event.model_dump()
    dumped["ts"] = datetime.now(UTC).isoformat()
    dumped["call_id"] = envelope_call_id
    try:
        await db.save_call_trace(envelope_call_id, [dumped])
    except Exception as e:
        raise TraceRecordError(str(e)) from e


async def apply_validated_move(
    *,
    db: DB,
    envelope_call: Call,
    move_type: MoveType,
    payload: dict[str, Any],
) -> MoveResult:
    """Execute a validated move against an already-opened envelope.

    Preconditions: ``payload`` has already been schema-validated for ``move_type``,
    the ``--accreting-only`` gate has already been checked, the envelope is open
    and live, and the caller owns the DB lifecycle (creation and cleanup).

    Raises ``TraceRecordError`` if the move lands in the DB but its trace event
    fails to record — the mutation is live but the envelope's frontend view is
    now incomplete.
    """
    move_def = MOVES.get(move_type)
    if move_def is None:
        raise ValueError(f"move {move_type} has no MoveDef in the registry")
    validated = move_def.schema(**payload)
    result = await move_def.execute(validated, envelope_call, db)

    created_ids: list[str] = []
    if result.created_page_id:
        created_ids.append(result.created_page_id)
        print_event("•", f"created page {result.created_page_id[:8]}")
    if result.extra_created_ids:
        for pid in result.extra_created_ids:
            created_ids.append(pid)
            print_event("•", f"also created {pid[:8]}")

    await _record_envelope_trace_event(
        db,
        envelope_call_id=envelope_call.id,
        move_type=move_type,
        validated_payload=validated,
        created_page_ids=created_ids,
        message=result.message,
    )
    return result


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "move_type",
        nargs="?",
        help="Move type (e.g. CREATE_QUESTION). See --list.",
    )
    parser.add_argument(
        "payload",
        nargs="?",
        help="JSON payload matching the move's schema",
    )
    parser.add_argument("--list", action="store_true", help="List available moves")
    parser.add_argument(
        "--schema",
        metavar="MOVE_TYPE",
        default=None,
        help=(
            "Print the full payload schema for one move (fields, types, "
            "required/optional, descriptions, and nested models) and exit."
        ),
    )
    parser.add_argument(
        "--scope",
        default=None,
        help="Optional scope question id (used when creating a new envelope)",
    )
    parser.add_argument(
        "--accreting-only",
        action="store_true",
        help=(
            "Refuse any move not in the accreting allowlist (CREATE_*, LINK_* "
            "excluding REMOVE_LINK, FLAG/REPORT, PROPOSE_CONCEPT, LOAD_PAGE)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the payload and print what would happen, without executing",
    )
    args = parser.parse_args()

    if args.list:
        _list_moves()
        return

    if args.schema:
        try:
            schema_move_type = MoveType(args.schema)
        except ValueError:
            print(
                f"unknown move type: {args.schema!r}. Use --list.",
                file=sys.stderr,
            )
            sys.exit(2)
        print(_render_schema(schema_move_type))
        return

    if not args.move_type or not args.payload:
        parser.error("move_type and payload are required (or use --list)")

    try:
        move_type = MoveType(args.move_type)
    except ValueError:
        print(f"unknown move type: {args.move_type!r}. Use --list.", file=sys.stderr)
        sys.exit(2)

    if args.accreting_only and move_type not in ACCRETING_MOVES:
        print(
            f"refusing {move_type.value}: not in the accreting allowlist. "
            "Drop --accreting-only to apply destructive/in-place moves.",
            file=sys.stderr,
        )
        sys.exit(2)

    move_def = MOVES.get(move_type)
    if move_def is None:
        print(f"move {move_type} has no MoveDef in the registry", file=sys.stderr)
        sys.exit(2)

    try:
        payload_dict = json.loads(args.payload)
    except json.JSONDecodeError as e:
        print(f"invalid JSON payload: {e}", file=sys.stderr)
        sys.exit(2)

    try:
        move_def.schema(**payload_dict)
    except Exception as e:
        print(f"payload validation failed: {e}", file=sys.stderr)
        print(file=sys.stderr)
        print(
            "Full schema for this move (required + optional fields):",
            file=sys.stderr,
        )
        print(_render_schema(move_type), file=sys.stderr)
        sys.exit(2)

    if args.dry_run:
        print(f"[dry-run] move: {move_type.value}")
        print(
            "[dry-run] accreting: "
            f"{'yes' if move_type in ACCRETING_MOVES else 'NO (destructive/in-place)'}"
        )
        print("[dry-run] payload:")
        print(_payload_preview(payload_dict))
        print("[dry-run] (not executed)")
        return

    db, call = await ensure_chat_envelope(scope_question_id=args.scope)
    try:
        print(f"envelope:  call={call.id[:8]} run={db.run_id[:8]}")
        print_trace(db.run_id, label="trace url")
        print_event("⚙", f"cc-mediated move: {move_type.value}")
        try:
            result = await apply_validated_move(
                db=db,
                envelope_call=call,
                move_type=move_type,
                payload=payload_dict,
            )
        except TraceRecordError as e:
            print(
                f"ERROR: move applied to DB but trace event failed to record: {e}",
                file=sys.stderr,
            )
            print(
                "       the envelope may be incomplete in the frontend; "
                f"inspect run {db.run_id}",
                file=sys.stderr,
            )
            sys.exit(1)

        print()
        print(result.message.rstrip())
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
