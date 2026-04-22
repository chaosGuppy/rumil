#!/usr/bin/env python3
"""Google Deep Research CLI — standalone tool.

Thin wrapper over ``rumil.deep_research``. For workspace-integrated runs
that produce a Source page, use the ``/rumil-deep-research`` skill instead.
"""

from __future__ import annotations

import argparse
import signal
import sys
from datetime import datetime
from pathlib import Path

from rumil import deep_research as dr


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Google Deep Research CLI")
    p.add_argument("prompt", nargs="?", help="Research question (omit to read from stdin)")
    p.add_argument("--agent", default=dr.DEFAULT_AGENT)
    p.add_argument(
        "--max",
        action="store_true",
        help=f"Shortcut for {dr.MAX_AGENT}",
    )
    p.add_argument("--collaborative-planning", action="store_true")
    p.add_argument("--thinking-summaries", choices=["auto", "none"], default="auto")
    p.add_argument("--no-visualization", action="store_true")
    p.add_argument("--out", type=Path, help="Output dir (default: output-<stamp>)")
    p.add_argument("--stream", action="store_true", help="Stream SSE events instead of polling")
    p.add_argument("--poll-interval", type=float, default=10.0)
    p.add_argument(
        "--resume",
        metavar="ID",
        help="Resume an existing interaction instead of creating one",
    )
    return p.parse_args()


def install_cancel_handler(client, interaction_id: str) -> None:
    def handler(signum, frame):
        print("\nCancelling interaction...", file=sys.stderr)
        try:
            dr.cancel(interaction_id, client=client)
        except Exception as e:
            print(f"  cancel failed: {e}", file=sys.stderr)
        sys.exit(130)

    signal.signal(signal.SIGINT, handler)


def main() -> None:
    args = parse_args()
    agent = dr.MAX_AGENT if args.max else args.agent
    prompt = args.prompt or sys.stdin.read().strip()
    if not prompt and not args.resume:
        print("No prompt provided (pass as arg, via stdin, or use --resume).", file=sys.stderr)
        sys.exit(2)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = args.out or Path(f"output-{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)
    if prompt:
        (out_dir / "prompt.txt").write_text(prompt + "\n")

    client = dr.make_client()
    agent_config = dr.build_agent_config(
        collaborative_planning=args.collaborative_planning,
        thinking_summaries=args.thinking_summaries,
        no_visualization=args.no_visualization,
    )

    if args.resume:
        interaction_id = args.resume
        print(f"Resuming interaction: {interaction_id}")
        install_cancel_handler(client, interaction_id)
        interaction = dr.poll_until_terminal(
            interaction_id,
            interval=args.poll_interval,
            client=client,
            on_status=lambda s: print(f"Status: {s}"),
        )
    elif args.stream:
        kwargs: dict = {"agent": agent}
        if agent_config is not None:
            kwargs["agent_config"] = agent_config
        stream = client.interactions.create(input=prompt, stream=True, **kwargs)
        events_path = out_dir / "events.jsonl"
        interaction_id = dr.stream_events(stream, events_path)
        if not interaction_id:
            print("Stream ended without yielding an interaction id.", file=sys.stderr)
            sys.exit(1)
        interaction = dr.get_interaction(interaction_id, client=client)
    else:
        interaction_id = dr.start_research(
            prompt,
            agent=agent,
            agent_config=agent_config,
            client=client,
        )
        print(f"Research started: {interaction_id}")
        install_cancel_handler(client, interaction_id)
        interaction = dr.poll_until_terminal(
            interaction_id,
            interval=args.poll_interval,
            client=client,
            on_status=lambda s: print(f"Status: {s}"),
        )

    artifacts = dr.save_artifacts(interaction, out_dir)
    print(f"Body:        {artifacts.body}")
    print(f"Annotations: {artifacts.annotations}")
    for img in artifacts.images:
        print(f"Image:       {img}")
    if artifacts.other_blocks:
        print(f"Other output blocks (skipped, see interaction.json): {artifacts.other_blocks}")
    usage = dr.usage_summary(interaction)
    if usage:
        print(f"Usage: {usage}")

    status = interaction.status
    if status != "completed":
        print(f"Final status: {status}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
