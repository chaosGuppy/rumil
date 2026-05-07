"""Tool registry + spawn-tool / finalize-tool / note-finding factory.

The mainline agent's toolkit is composed at run time:
- one spawn tool per ``SubroutineDef`` in the config's library
- a ``finalize`` tool emitting the final answer
- a ``note_finding`` tool for in-thread scratchpad

The same registry is reused by ``FreeformAgentSubroutine`` to wire its
own tools at spawn time. Registered tool factories produce
:class:`rumil.llm.Tool` instances given a ``SpawnCtx``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

from rumil.llm import Tool

if TYPE_CHECKING:
    from rumil.orchestrators.simple_spine.subroutines.base import SpawnCtx


ToolFactory = Callable[["SpawnCtx"], Tool]

_REGISTRY: dict[str, ToolFactory] = {}


def register_tool(name: str, factory: ToolFactory) -> None:
    """Register a tool factory under ``name``.

    Idempotent: re-registering the same name silently overwrites — this
    keeps test fixtures and module-level register calls safe to re-run.
    """
    _REGISTRY[name] = factory


def resolve_tools(names: Sequence[str], ctx: SpawnCtx) -> list[Tool]:
    """Build a list of :class:`Tool` instances for the given names.

    Unknown names raise — silent omission would let a config typo make
    a tool quietly disappear from the spawned agent's toolkit.
    """
    out: list[Tool] = []
    for n in names:
        factory = _REGISTRY.get(n)
        if factory is None:
            available = sorted(_REGISTRY)
            raise KeyError(f"unknown tool {n!r}; registered tools: {available}")
        out.append(factory(ctx))
    return out


def make_finalize_tool(
    on_finalize: Callable[[str], Awaitable[str]],
) -> Tool:
    """Build the ``finalize`` tool the mainline agent calls to terminate.

    ``on_finalize`` receives the answer text and returns a tool-result
    string (typically a confirmation; the orchestrator inspects state
    set by ``on_finalize`` to decide whether to break the loop).
    """

    async def fn(args: dict) -> str:
        answer = str(args.get("answer", "")).strip()
        if not answer:
            return "Error: finalize requires a non-empty `answer` field."
        return await on_finalize(answer)

    return Tool(
        name="finalize",
        description=(
            "Emit the final deliverable for this run and terminate the loop. "
            "Call this when you have produced the answer that satisfies the "
            "output guidance, when further spawns would not improve the "
            "deliverable, or when budget pressure forces it. Pass the full "
            "answer text in `answer` — the harness extracts it verbatim."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "description": "The full final deliverable text.",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief note on why finalizing now (for the trace).",
                },
            },
            "required": ["answer"],
            "additionalProperties": False,
        },
        fn=fn,
    )


def make_note_finding_tool(
    on_note: Callable[[str], Awaitable[str]],
) -> Tool:
    """Build the ``note_finding`` scratchpad tool.

    Notes are written to the trace and aggregated on the orch result;
    they do NOT touch the workspace page graph.
    """

    async def fn(args: dict) -> str:
        text = str(args.get("text", "")).strip()
        if not text:
            return "Error: note_finding requires non-empty `text`."
        return await on_note(text)

    return Tool(
        name="note_finding",
        description=(
            "Record an interim finding to the run's note list. Use for "
            "intermediate beliefs, partial conclusions, or reminders to "
            "future-you in this same run. Notes are written to the trace "
            "and returned alongside the final answer; they do NOT modify "
            "the workspace."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The note text.",
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        fn=fn,
    )
