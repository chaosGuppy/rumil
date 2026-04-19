"""CLI for mid-run human steering.

Every subcommand produces a ``run_nudges`` row via the NudgeStore.
The orchestrator + per-call context build consume active nudges at
safe points (phases 3-4). The same primitive backs parma chat, inline
UI affordances, and the /rumil-nudge skill.

Usage:
    # Soft note injected into context newest-first
    uv run python scripts/nudge.py <run_id> note "focus on the economic angle"

    # Constrain dispatch: hard-ban certain call types
    uv run python scripts/nudge.py <run_id> constrain --ban-types web_research,assess
    uv run python scripts/nudge.py <run_id> constrain --ban-types web_research --persistent --expires-after-n 3

    # Scope a note to specific questions
    uv run python scripts/nudge.py <run_id> note "watch out for sample bias" --questions Q1,Q2

    # Veto or redo a specific call
    uv run python scripts/nudge.py <run_id> veto <call_id>
    uv run python scripts/nudge.py <run_id> redo <call_id>

    # Rewrite the root question's framing (overlay-only in v1)
    uv run python scripts/nudge.py <run_id> rewrite "Really what we want is: does X hold under Y?"

    # Pause / resume — also writes runs.paused_at
    uv run python scripts/nudge.py <run_id> pause
    uv run python scripts/nudge.py <run_id> resume

    # List / revoke
    uv run python scripts/nudge.py <run_id> list
    uv run python scripts/nudge.py <run_id> list --status all
    uv run python scripts/nudge.py revoke <nudge_id>

All subcommands accept ``--author claude`` to mark the nudge as authored
by Claude (via skill) rather than a human directly. Default is ``human``.
"""

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime

from rumil.database import DB
from rumil.models import (
    NudgeAuthorKind,
    NudgeDurability,
    NudgeKind,
    NudgeScope,
    NudgeStatus,
    RunNudge,
)


def _csv(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def _scope_from_args(args: argparse.Namespace) -> NudgeScope:
    return NudgeScope(
        call_types=_csv(getattr(args, "ban_types", None) or getattr(args, "call_types", None)),
        question_ids=_csv(getattr(args, "questions", None)),
        call_id=getattr(args, "call_id", None),
        expires_after_n_calls=getattr(args, "expires_after_n", None),
    )


def _durability(args: argparse.Namespace) -> NudgeDurability:
    return (
        NudgeDurability.PERSISTENT
        if getattr(args, "persistent", False)
        else NudgeDurability.ONE_SHOT
    )


def _author(args: argparse.Namespace) -> NudgeAuthorKind:
    return NudgeAuthorKind(getattr(args, "author", "human"))


def _print_nudge(n: RunNudge) -> None:
    scope_parts: list[str] = []
    if n.scope.call_types:
        scope_parts.append(f"call_types={','.join(n.scope.call_types)}")
    if n.scope.question_ids:
        scope_parts.append(f"questions={','.join(n.scope.question_ids)}")
    if n.scope.call_id:
        scope_parts.append(f"call_id={n.scope.call_id}")
    if n.scope.expires_after_n_calls is not None:
        scope_parts.append(f"expires_after_n={n.scope.expires_after_n_calls}")
    scope_str = " ".join(scope_parts) or "-"
    flags = "hard" if n.hard else "soft"
    flags = f"{flags},{n.durability.value}"
    soft = (n.soft_text or "").replace("\n", " ")
    if len(soft) > 80:
        soft = soft[:77] + "..."
    print(
        f"{n.id[:8]}  {n.kind.value:<20}  {n.status.value:<8}  {flags:<16}  "
        f"scope=[{scope_str}]  {soft}"
    )


async def _cmd_create(
    db: DB,
    run_id: str,
    *,
    kind: NudgeKind,
    durability: NudgeDurability,
    author_kind: NudgeAuthorKind,
    author_note: str = "",
    payload: dict | None = None,
    scope: NudgeScope | None = None,
    soft_text: str | None = None,
    hard: bool = False,
) -> RunNudge:
    nudge = await db.nudges.create_nudge(
        run_id=run_id,
        kind=kind,
        durability=durability,
        author_kind=author_kind,
        author_note=author_note,
        payload=payload,
        scope=scope,
        soft_text=soft_text,
        hard=hard,
    )
    _print_nudge(nudge)
    return nudge


async def _cmd_note(db: DB, args: argparse.Namespace) -> None:
    await _cmd_create(
        db,
        args.run_id,
        kind=NudgeKind.INJECT_NOTE,
        durability=_durability(args),
        author_kind=_author(args),
        scope=_scope_from_args(args),
        soft_text=args.text,
        hard=False,
    )


async def _cmd_constrain(db: DB, args: argparse.Namespace) -> None:
    if not args.ban_types:
        print("constrain requires --ban-types", file=sys.stderr)
        sys.exit(2)
    await _cmd_create(
        db,
        args.run_id,
        kind=NudgeKind.CONSTRAIN_DISPATCH,
        durability=_durability(args),
        author_kind=_author(args),
        scope=_scope_from_args(args),
        soft_text=args.reason,
        hard=True,
    )


async def _cmd_veto(db: DB, args: argparse.Namespace) -> None:
    await _cmd_create(
        db,
        args.run_id,
        kind=NudgeKind.VETO_CALL,
        durability=NudgeDurability.ONE_SHOT,
        author_kind=_author(args),
        scope=NudgeScope(call_id=args.call_id),
        soft_text=args.reason,
        hard=True,
    )


async def _cmd_redo(db: DB, args: argparse.Namespace) -> None:
    await _cmd_create(
        db,
        args.run_id,
        kind=NudgeKind.REDO_CALL,
        durability=NudgeDurability.ONE_SHOT,
        author_kind=_author(args),
        scope=NudgeScope(call_id=args.call_id),
        soft_text=args.reason,
        hard=True,
    )


async def _cmd_rewrite(db: DB, args: argparse.Namespace) -> None:
    await _cmd_create(
        db,
        args.run_id,
        kind=NudgeKind.REWRITE_GOAL,
        durability=NudgeDurability.PERSISTENT,
        author_kind=_author(args),
        scope=_scope_from_args(args),
        soft_text=args.text,
        hard=False,
    )


async def _cmd_pause(db: DB, args: argparse.Namespace) -> None:
    nudge = await _cmd_create(
        db,
        args.run_id,
        kind=NudgeKind.PAUSE,
        durability=NudgeDurability.PERSISTENT,
        author_kind=_author(args),
        soft_text=args.reason,
        hard=True,
    )
    await db._execute(
        db.client.table("runs")
        .update({"paused_at": datetime.now(UTC).isoformat()})
        .eq("id", args.run_id)
    )
    print(f"runs.paused_at updated for {args.run_id[:8]} (nudge {nudge.id[:8]})", file=sys.stderr)


async def _cmd_resume(db: DB, args: argparse.Namespace) -> None:
    active = await db.nudges.list_nudges_for_run(args.run_id, status=NudgeStatus.ACTIVE)
    pause_nudges = [n for n in active if n.kind == NudgeKind.PAUSE]
    for n in pause_nudges:
        await db.nudges.revoke_nudge(n.id)
    await db._execute(db.client.table("runs").update({"paused_at": None}).eq("id", args.run_id))
    print(
        f"runs.paused_at cleared for {args.run_id[:8]} ({len(pause_nudges)} pause nudge(s) revoked)",
        file=sys.stderr,
    )


async def _cmd_list(db: DB, args: argparse.Namespace) -> None:
    status: NudgeStatus | None
    if args.status == "all":
        status = None
    else:
        status = NudgeStatus(args.status)
    nudges = await db.nudges.list_nudges_for_run(args.run_id, status=status)
    if not nudges:
        print("(no nudges)")
        return
    for n in nudges:
        _print_nudge(n)


async def _cmd_revoke(db: DB, args: argparse.Namespace) -> None:
    existing = await db.nudges.get_nudge(args.nudge_id)
    if existing is None:
        print(f"Nudge {args.nudge_id} not found", file=sys.stderr)
        sys.exit(1)
    if existing.status != NudgeStatus.ACTIVE:
        print(
            f"Nudge {args.nudge_id[:8]} is {existing.status.value}, not active",
            file=sys.stderr,
        )
        sys.exit(1)
    refreshed = await db.nudges.revoke_nudge(args.nudge_id)
    if refreshed is not None:
        _print_nudge(refreshed)


def _add_scope_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--questions", help="Comma-separated question ids to scope this nudge to")
    p.add_argument(
        "--expires-after-n",
        type=int,
        help="Expire after being applied to N calls",
    )


def _add_common_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--persistent",
        action="store_true",
        help="Durability=persistent (default is one_shot)",
    )
    p.add_argument(
        "--author",
        choices=[e.value for e in NudgeAuthorKind],
        default=NudgeAuthorKind.HUMAN.value,
        help="Who authored this nudge (default: human)",
    )


def _build_verb_parser() -> argparse.ArgumentParser:
    """Parser for `<run_id> <verb> ...` form. Called after we've pulled run_id."""
    parser = argparse.ArgumentParser(description="Nudge verb for a run")
    verbs = parser.add_subparsers(dest="verb", required=True)

    note_p = verbs.add_parser("note", help="Inject a soft NL note into context")
    note_p.add_argument("text")
    _add_scope_flags(note_p)
    _add_common_flags(note_p)
    note_p.set_defaults(_handler=_cmd_note)

    cons_p = verbs.add_parser("constrain", help="Hard constraint on dispatch")
    cons_p.add_argument("--ban-types", help="Comma-separated CallType values to block")
    cons_p.add_argument("--reason", help="Optional free-text rationale (rendered in context)")
    _add_scope_flags(cons_p)
    _add_common_flags(cons_p)
    cons_p.set_defaults(_handler=_cmd_constrain)

    veto_p = verbs.add_parser("veto", help="Veto a specific call (won't be redispatched)")
    veto_p.add_argument("call_id")
    veto_p.add_argument("--reason")
    _add_common_flags(veto_p)
    veto_p.set_defaults(_handler=_cmd_veto)

    redo_p = verbs.add_parser("redo", help="Mark a completed call to be redone")
    redo_p.add_argument("call_id")
    redo_p.add_argument("--reason")
    _add_common_flags(redo_p)
    redo_p.set_defaults(_handler=_cmd_redo)

    rewrite_p = verbs.add_parser("rewrite", help="Rewrite the root goal (overlay-only in v1)")
    rewrite_p.add_argument("text")
    _add_scope_flags(rewrite_p)
    _add_common_flags(rewrite_p)
    rewrite_p.set_defaults(_handler=_cmd_rewrite)

    pause_p = verbs.add_parser("pause", help="Pause the run (writes runs.paused_at)")
    pause_p.add_argument("--reason")
    _add_common_flags(pause_p)
    pause_p.set_defaults(_handler=_cmd_pause)

    resume_p = verbs.add_parser("resume", help="Resume a paused run")
    resume_p.set_defaults(_handler=_cmd_resume)

    list_p = verbs.add_parser("list", help="List nudges on the run")
    list_p.add_argument(
        "--status",
        default=NudgeStatus.ACTIVE.value,
        choices=[*[e.value for e in NudgeStatus], "all"],
    )
    list_p.set_defaults(_handler=_cmd_list)

    return parser


async def _run(argv: list[str]) -> None:
    prod = False
    if argv and argv[0] == "--prod":
        prod = True
        argv = argv[1:]

    if not argv:
        print(__doc__, file=sys.stderr)
        sys.exit(2)

    if argv[0] == "revoke":
        if len(argv) < 2:
            print("revoke requires <nudge_id>", file=sys.stderr)
            sys.exit(2)
        nudge_id = argv[1]
        db = await DB.create(run_id=str(uuid.uuid4()), prod=prod)
        args = argparse.Namespace(nudge_id=nudge_id)
        await _cmd_revoke(db, args)
        return

    run_id = argv[0]
    verb_args = _build_verb_parser().parse_args(argv[1:])
    verb_args.run_id = run_id
    db = await DB.create(run_id=str(uuid.uuid4()), prod=prod)
    run = await db.get_run(run_id)
    if run is None:
        print(f"Run {run_id} not found", file=sys.stderr)
        sys.exit(1)
    db.project_id = run.get("project_id") or ""
    await verb_args._handler(db, verb_args)


def main() -> None:
    asyncio.run(_run(sys.argv[1:]))


if __name__ == "__main__":
    main()
