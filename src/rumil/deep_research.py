"""Google Deep Research client — reusable by CLI and skill.

Thin wrapper over ``google.genai`` for the ``deep-research-*`` agents.
Exposes the start/poll/save loop as functions so callers can fire a run
in the background, check status later, and persist artifacts when done.

SDK reference (ai.google.dev page 404s; source is the canonical doc):
  https://github.com/googleapis/python-genai/tree/main/google/genai/_interactions
"""

from __future__ import annotations

import base64
import json
import mimetypes
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google import genai

DEFAULT_AGENT = "deep-research-preview-04-2026"
MAX_AGENT = "deep-research-max-preview-04-2026"
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "incomplete"})


@dataclass
class Artifacts:
    """Paths + metadata produced by ``save_artifacts``."""

    interaction_json: Path
    body: Path
    annotations: Path
    images: list[Path]
    other_blocks: list[tuple[int, str | None]]
    body_text: str


def build_agent_config(
    *,
    collaborative_planning: bool = False,
    thinking_summaries: str = "auto",
    no_visualization: bool = False,
) -> dict[str, Any] | None:
    overrides: dict[str, Any] = {}
    if collaborative_planning:
        overrides["collaborative_planning"] = True
    if thinking_summaries != "auto":
        overrides["thinking_summaries"] = thinking_summaries
    if no_visualization:
        overrides["visualization"] = "off"
    return {"type": "deep-research", **overrides} if overrides else None


def make_client() -> genai.Client:
    return genai.Client()


def start_research(
    prompt: str,
    *,
    agent: str = DEFAULT_AGENT,
    agent_config: dict[str, Any] | None = None,
    client: genai.Client | None = None,
) -> str:
    """Kick off a background research interaction. Returns the interaction id."""
    c = client or make_client()
    kwargs: dict[str, Any] = {"agent": agent}
    if agent_config is not None:
        kwargs["agent_config"] = agent_config
    interaction = c.interactions.create(input=prompt, background=True, **kwargs)
    return interaction.id


def get_interaction(interaction_id: str, *, client: genai.Client | None = None) -> Any:
    c = client or make_client()
    return c.interactions.get(interaction_id)


def cancel(interaction_id: str, *, client: genai.Client | None = None) -> None:
    c = client or make_client()
    c.interactions.cancel(interaction_id)


def poll_until_terminal(
    interaction_id: str,
    *,
    interval: float = 10.0,
    timeout_seconds: float = 7200.0,
    client: genai.Client | None = None,
    on_status: Callable[[str], None] | None = None,
) -> Any:
    """Block until the interaction reaches a terminal (or requires_action) status.

    Calls ``on_status(status)`` each time the status changes. ``requires_action``
    returns the current state rather than blocking forever (collaborative planning).
    Raises ``TimeoutError`` if the run stays non-terminal past ``timeout_seconds``.
    """
    c = client or make_client()
    last_status = None
    deadline = time.monotonic() + timeout_seconds
    while True:
        interaction = c.interactions.get(interaction_id)
        status = interaction.status
        if status != last_status:
            if on_status is not None:
                on_status(status)
            last_status = status
        if status in TERMINAL_STATUSES or status == "requires_action":
            return interaction
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"interaction {interaction_id} still at status '{status}' "
                f"after {timeout_seconds:.0f}s"
            )
        time.sleep(interval)


def stream_events(stream: Iterable[Any], events_path: Path) -> str | None:
    """Consume an SSE stream, writing raw events to disk and printing deltas.

    Returns the interaction id once observed, or None if the stream ended
    without yielding one. CLI-only helper — the skill lane uses background mode.
    """
    interaction_id: str | None = None
    with events_path.open("w") as f:
        for event in stream:
            try:
                line = event.model_dump_json()
            except AttributeError:
                line = json.dumps({"repr": repr(event)})
            f.write(line + "\n")
            f.flush()

            etype = getattr(event, "event_type", None) or getattr(event, "type", None)
            if etype in ("interaction.start", "interaction.created"):
                iobj = getattr(event, "interaction", None) or getattr(event, "data", None)
                if iobj is not None:
                    interaction_id = getattr(iobj, "id", interaction_id)
                    if interaction_id:
                        print(f"Research started: {interaction_id}")
            elif etype == "content.delta":
                delta = getattr(event, "delta", None) or getattr(event, "text", None)
                if isinstance(delta, str):
                    sys.stdout.write(delta)
                    sys.stdout.flush()
            elif etype == "interaction.status_update":
                status = getattr(event, "status", None)
                if status:
                    print(f"\nStatus: {status}")
            elif etype == "error":
                err = getattr(event, "error", None)
                print(f"\nError event: {err}", file=sys.stderr)
            elif etype == "interaction.complete":
                print("\nInteraction complete.")
    return interaction_id


def save_artifacts(interaction: Any, out_dir: Path) -> Artifacts:
    """Write body.md, annotations.json, interaction.json, and any images.

    Returns the resolved paths plus the concatenated body text (useful for
    callers that want to create a Source page without re-reading the file).
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    interaction_path = out_dir / "interaction.json"
    try:
        interaction_path.write_text(interaction.model_dump_json(indent=2))
    except AttributeError:
        interaction_path.write_text(repr(interaction))

    bodies: list[str] = []
    images: list[Path] = []
    other: list[tuple[int, str | None]] = []
    for i, o in enumerate(interaction.outputs or []):
        otype = getattr(o, "type", None)
        text = getattr(o, "text", None)
        if text:
            bodies.append(text)
        elif otype == "image":
            mime = getattr(o, "mime_type", None) or "image/png"
            ext = mimetypes.guess_extension(mime) or ".bin"
            path = out_dir / f"image-{i}{ext}"
            data = getattr(o, "data", None)
            if isinstance(data, str):
                path.write_bytes(base64.b64decode(data))
            elif isinstance(data, bytes | bytearray):
                path.write_bytes(data)
            images.append(path)
        else:
            other.append((i, otype))

    body_text = "\n".join(bodies)
    body_path = out_dir / "body.md"
    body_path.write_text(body_text)

    annotations = [
        ann for o in (interaction.outputs or []) for ann in (getattr(o, "annotations", None) or [])
    ]
    annotations_path = out_dir / "annotations.json"
    try:
        annotations_path.write_text(
            json.dumps(
                [a.model_dump() if hasattr(a, "model_dump") else a for a in annotations],
                indent=2,
            )
        )
    except TypeError:
        annotations_path.write_text(json.dumps([repr(a) for a in annotations], indent=2))

    return Artifacts(
        interaction_json=interaction_path,
        body=body_path,
        annotations=annotations_path,
        images=images,
        other_blocks=other,
        body_text=body_text,
    )


def usage_summary(interaction: Any) -> str | None:
    usage = getattr(interaction, "usage", None)
    if not usage:
        return None
    fields = [
        "total_input_tokens",
        "total_output_tokens",
        "total_thought_tokens",
        "total_tool_use_tokens",
        "total_cached_tokens",
        "total_tokens",
    ]
    parts = [f"{f}={getattr(usage, f)}" for f in fields if getattr(usage, f, None) is not None]
    return ", ".join(parts) if parts else None
