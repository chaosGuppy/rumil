"""Fire a Google Deep Research run and (on resume) persist the result as a Source page.

Three modes, mirroring the conversation-driven flow:

    - fire (default):      start a DR run in the background, print the interaction
                           id, and return immediately. State is stashed under
                           .claude/state/deep-research/<id>/ so --resume can
                           pick it up later with no extra args.
    - --wait <prompt>:     fire *and* block until terminal, then save. Convenient
                           for short prompts. Ctrl-C disconnects without cancelling.
    - --check <id>:        one-shot status probe — no polling, no side effects.
    - --resume <id>:       poll until terminal, write body.md / interaction.json
                           / annotations.json to the state dir, create a Source
                           page from body.md. --for (if recorded at fire time)
                           is propagated into the Source's extra.

Usage:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.deep_research \\
        "<prompt>" [--for <q_id>] [--max] [--wait]
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.deep_research --resume <id>
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.deep_research --check <id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rumil import deep_research as dr
from rumil.sources import create_source_page_from_text

from ._format import print_event, truncate
from ._runctx import make_db, resolve_workspace

STATE_ROOT = Path(".claude/state/deep-research")
POLL_INTERVAL = 15.0


def _state_dir(interaction_id: str) -> Path:
    return STATE_ROOT / interaction_id


def _write_meta(state_dir: Path, meta: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "meta.json").write_text(json.dumps(meta, indent=2))


def _read_meta(state_dir: Path) -> dict[str, Any]:
    path = state_dir / "meta.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _disconnect_on_sigint() -> None:
    """Ctrl-C during --wait detaches instead of cancelling the remote run."""

    def handler(signum, frame):
        print("\nDisconnecting — the interaction keeps running.", file=sys.stderr)
        print("Resume with: /rumil-deep-research --resume <id>", file=sys.stderr)
        sys.exit(130)

    signal.signal(signal.SIGINT, handler)


async def _fire(args: argparse.Namespace) -> None:
    prompt = args.prompt
    if not prompt:
        print("error: prompt required (pass as positional arg)", file=sys.stderr)
        sys.exit(2)

    agent = dr.MAX_AGENT if args.max else dr.DEFAULT_AGENT
    agent_config = dr.build_agent_config(
        collaborative_planning=args.collaborative_planning,
        thinking_summaries=args.thinking_summaries,
        no_visualization=args.no_visualization,
    )

    client = dr.make_client()
    interaction_id = await asyncio.to_thread(
        dr.start_research,
        prompt,
        agent=agent,
        agent_config=agent_config,
        client=client,
    )

    resolved_ws = resolve_workspace(args.workspace)
    state_dir = _state_dir(interaction_id)
    _write_meta(
        state_dir,
        {
            "interaction_id": interaction_id,
            "prompt": prompt,
            "agent": agent,
            "agent_config": agent_config,
            "for_question": args.for_question,
            "workspace": resolved_ws,
            "started_at": datetime.now(UTC).isoformat(),
        },
    )
    (state_dir / "prompt.txt").write_text(prompt + "\n")

    print_event("→", f"deep research started: {interaction_id}")
    print_event("•", f"agent: {agent}")
    if args.for_question:
        print_event("•", f"tagged for question: {args.for_question}")
    print_event("•", f"state dir: {state_dir}")

    if args.wait:
        _disconnect_on_sigint()
        await _finalize(interaction_id, client=client)
    else:
        print()
        print(f"Run `/rumil-deep-research --resume {interaction_id}` when ready.")
        print(f"Or peek at status: `/rumil-deep-research --check {interaction_id}`.")


async def _check(interaction_id: str) -> None:
    interaction = await asyncio.to_thread(dr.get_interaction, interaction_id)
    status = interaction.status
    print_event("•", f"interaction {interaction_id}")
    print_event("•", f"status: {status}")
    usage = dr.usage_summary(interaction)
    if usage:
        print_event("•", f"usage: {usage}")
    if status in dr.TERMINAL_STATUSES:
        print()
        print(f"Run `/rumil-deep-research --resume {interaction_id}` to save as a Source.")


async def _resume(interaction_id: str, *, workspace_override: str | None) -> None:
    await _finalize(interaction_id, workspace_override=workspace_override)


async def _finalize(
    interaction_id: str,
    *,
    client: Any | None = None,
    workspace_override: str | None = None,
) -> None:
    """Poll → save artifacts → create Source page. Reads meta from state dir."""
    state_dir = _state_dir(interaction_id)
    meta = _read_meta(state_dir)
    # meta can be empty when resuming an interaction that was fired outside the skill
    # (e.g. via scripts/run_deep_research.py). That's fine — we just lose the --for tag.

    print_event("•", f"polling {interaction_id} (interval {POLL_INTERVAL:.0f}s)")
    interaction = await asyncio.to_thread(
        dr.poll_until_terminal,
        interaction_id,
        interval=POLL_INTERVAL,
        client=client,
        on_status=lambda s: print_event("•", f"status: {s}"),
    )

    artifacts = await asyncio.to_thread(dr.save_artifacts, interaction, state_dir)
    status = interaction.status
    print_event("•", f"terminal status: {status}")
    if status != "completed":
        print_event("✗", "run did not complete — Source not created")
        print(f"inspect: {artifacts.interaction_json}")
        sys.exit(1)

    body_text = artifacts.body_text
    if not body_text.strip():
        print_event("✗", "run completed but no text output — Source not created")
        sys.exit(1)

    ws_override = workspace_override or meta.get("workspace")
    db, ws = await make_db(workspace=ws_override)
    try:
        label = _derive_label(meta.get("prompt"), interaction_id)
        agent = meta.get("agent") or getattr(interaction, "agent", None)
        extra: dict[str, Any] = {
            "source_kind": "deep_research",
            "interaction_id": interaction_id,
            "agent": agent,
        }
        for_q = meta.get("for_question")
        if for_q:
            extra["for_question"] = for_q
        annotations = _load_annotations(artifacts.annotations)
        if annotations:
            extra["annotations"] = annotations
        usage = dr.usage_summary(interaction)
        if usage:
            extra["usage"] = usage

        print(f"workspace: {ws}")
        page = await create_source_page_from_text(body_text, label, db, extra=extra)
        print_event("✓", f"saved source {page.id[:8]}  ({page.id})")
        print(f"headline:  {truncate(page.headline or '', 200)}")
        if for_q:
            print()
            print(f"Next: /rumil-ingest --from-page {page.id[:8]} --for {for_q}")
    finally:
        await db.close()


def _derive_label(prompt: str | None, interaction_id: str) -> str:
    base = (prompt or "deep research").strip().splitlines()[0]
    return f"deep-research: {truncate(base, 90)} [{interaction_id[-12:]}]"


def _load_annotations(path: Path) -> Sequence[Any]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", nargs="?", help="Research prompt (fire mode)")
    parser.add_argument("--for", dest="for_question", default=None, help="Tag run for a question")
    parser.add_argument("--max", action="store_true", help=f"Use {dr.MAX_AGENT}")
    parser.add_argument("--collaborative-planning", action="store_true")
    parser.add_argument("--thinking-summaries", choices=["auto", "none"], default="auto")
    parser.add_argument("--no-visualization", action="store_true")
    parser.add_argument("--wait", action="store_true", help="Block until terminal + save")
    parser.add_argument("--resume", metavar="ID", help="Poll a prior run and save as a Source")
    parser.add_argument("--check", metavar="ID", help="One-shot status probe")
    parser.add_argument("--workspace", default=None)
    args = parser.parse_args()

    modes = sum(bool(x) for x in (args.resume, args.check, args.prompt))
    if modes != 1:
        print(
            "error: pass exactly one of <prompt>, --resume <id>, --check <id>",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.check:
        await _check(args.check)
    elif args.resume:
        await _resume(args.resume, workspace_override=args.workspace)
    else:
        await _fire(args)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
