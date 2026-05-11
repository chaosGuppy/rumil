"""Tool definitions for axon — mainline surface + per-delegate finalize.

Mainline surface is intentionally tiny and stable across the run so
prompt cache stays valid forever:

- ``delegate``: fixed schema (:class:`DelegateRequest`); fn is a no-op
  because the orchestrator intercepts the call to run the configure +
  inner-loop dance.
- ``configure``: fixed schema (:class:`DelegateConfig`); fn is a no-op
  for the same reason — only valid in configure follow-up turns.
- ``finalize``: universal terminator. Mainline gets a default schema
  (freeform answer text). Each inner loop gets a finalize tool whose
  input_schema was set by configure.
- Direct tools (``web_research``, ``workspace_lookup``, ...): bounded
  I/O surfaces registered via :func:`register_direct_tool`. Resolved
  for both mainline and for any inner loop that lists them in
  ``DelegateConfig.tools``.

Tool *fns* on delegate / configure / finalize raise unconditionally if
called — they're handled at a higher layer in the orchestrator and the
fn would only be invoked if a code path leaks (which we want loud).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from rumil.llm import Tool
from rumil.orchestrators.axon.schemas import DelegateConfig, DelegateRequest

log = logging.getLogger(__name__)


DELEGATE_TOOL_NAME = "delegate"
CONFIGURE_TOOL_NAME = "configure"
FINALIZE_TOOL_NAME = "finalize"


DELEGATE_DESCRIPTION = (
    "Delegate a focused sub-task to a configured delegate agent. After "
    "you call this, you'll be asked to produce a full configuration "
    "for it by calling `configure` in the next turn — a follow-up "
    "where you'll see this same conversation plus a directive "
    "identifying which delegate to configure. The delegate runs with "
    "that config, terminates by calling `finalize`, and its result "
    "lands here as this call's tool_result.\n"
    "\n"
    "Two regimes:\n"
    " - inherit_context=True: the delegate inherits this conversation "
    "as its prefix and uses the same tools and system prompt I'm using. "
    "Cache-shared continuation. Use when you want the delegate to be a "
    "forked branch of your own thinking (distill recent discussion, "
    "steelman a current stance, continue exploring a thread).\n"
    " - inherit_context=False: the delegate starts fresh; configure "
    "may pick any system prompt and any tools. Cold start but bounded. "
    "Use when you want a different stance or scope than your current "
    "thread (a critic, a child investigation, a focused compute step).\n"
    "\n"
    "budget_usd is your commitment of cost out of the run's remaining "
    "budget — choose deliberately. configure cannot override.\n"
    "\n"
    "n>1 runs n independent samples in parallel with the same configure "
    "output. Results come back as a list."
)


CONFIGURE_DESCRIPTION = (
    "Produce the full DelegateConfig for the delegate identified in "
    "your follow-up directive. Set system_prompt and tools to None when "
    "the parent delegate had inherit_context=True (cache-shared "
    "continuation requires reusing the spine's system + tools). When "
    "inherit_context=False, you may pick any system_prompt and any "
    "tools subset. Set finalize_schema to whatever shape the result "
    "should come back as. side_effects controls persistence beyond the "
    "tool_result — currently only `write_artifact` is supported, which "
    "requires artifact_key. (Workspace page creation happens inside "
    "the delegate via the create_page tool when configured, not as a "
    "side effect.)\n\n"
    "Pass content into the delegate two ways:\n"
    " - extra_context (string): freeform prose — instructions, scratch "
    "notes, framing — appended to the delegate's first user message.\n"
    " - artifact_keys (list of strings): keys from the run's artifact "
    "store to splice into the delegate's first user message as "
    "XML-fenced <artifact key=...> blocks. Most useful in isolation "
    "regime where the delegate doesn't see your artifact view; also "
    "valid in continuation. Available keys are listed at run start in "
    "your system prompt (operating_assumptions plus any caller-seeded "
    "keys) and grow as sibling delegates write artifacts via the "
    "write_artifact side effect. Typo'd keys trigger a corrective "
    "retry — pick from existing keys.\n\n"
    "rationale is for the trace; one or two sentences."
)


DEFAULT_FINALIZE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {
            "type": "string",
            "description": "The final deliverable text.",
        },
        "reason": {
            "type": "string",
            "description": "Brief note on why finalizing now (for the trace).",
        },
    },
    "required": ["answer"],
    "additionalProperties": False,
}


def _delegate_request_input_schema() -> dict[str, Any]:
    return DelegateRequest.model_json_schema()


def _configure_input_schema() -> dict[str, Any]:
    return DelegateConfig.model_json_schema()


async def _intercept_only_fn(args: dict) -> str:
    """Stand-in fn for tools whose calls the orchestrator intercepts.

    If the orchestrator's loop ever lets one of these reach the standard
    fn dispatch path, we want a loud error rather than silent ack.
    """
    raise RuntimeError(
        "axon: delegate/configure/finalize tool fn invoked directly — "
        "orchestrator should have intercepted this call. Bug."
    )


def build_delegate_tool() -> Tool:
    """The single mainline-facing delegate primitive.

    Schema is :class:`DelegateRequest`. Stable across the run.
    """
    return Tool(
        name=DELEGATE_TOOL_NAME,
        description=DELEGATE_DESCRIPTION,
        input_schema=_delegate_request_input_schema(),
        fn=_intercept_only_fn,
    )


def build_configure_tool() -> Tool:
    """The configure tool used in configure follow-up turns.

    Defined in mainline's tool list from run start so its presence
    doesn't churn the cache. The model is instructed to call it only
    when prompted by a configure directive.
    """
    return Tool(
        name=CONFIGURE_TOOL_NAME,
        description=CONFIGURE_DESCRIPTION,
        input_schema=_configure_input_schema(),
        fn=_intercept_only_fn,
    )


def build_finalize_tool(
    input_schema: dict[str, Any] | None = None,
    description: str | None = None,
) -> Tool:
    """Build a finalize tool with the given input schema.

    For mainline, called with ``input_schema=None`` to use
    :data:`DEFAULT_FINALIZE_SCHEMA` (freeform answer text). For inner
    loops, the orchestrator builds a finalize tool per-delegate with
    the schema configure produced.
    """
    schema = input_schema if input_schema is not None else DEFAULT_FINALIZE_SCHEMA
    desc = description or (
        "Emit the final deliverable and terminate this loop. The fields "
        "specified in the input schema are the shape your caller expects "
        "the result to come back as."
    )
    return Tool(
        name=FINALIZE_TOOL_NAME,
        description=desc,
        input_schema=schema,
        fn=_intercept_only_fn,
    )


# Direct-tool registry: bounded I/O surfaces (web_research, workspace_lookup,
# ...) mainline can call without going through delegate. Tool factories
# take no args — everything they need is closed over at registration.

ToolFactory = Callable[[], Tool]
_DIRECT_TOOLS: dict[str, ToolFactory] = {}


def register_direct_tool(name: str, factory: ToolFactory) -> None:
    """Register a direct-tool factory under ``name``.

    Idempotent overwrite — safe to call from module init and tests.
    """
    _DIRECT_TOOLS[name] = factory


def resolve_direct_tools(names: tuple[str, ...] | list[str]) -> list[Tool]:
    """Build :class:`Tool` instances for the given direct-tool names.

    Unknown names raise — silent omission would let a YAML typo make a
    tool quietly disappear from the agent's toolkit.
    """
    out: list[Tool] = []
    for n in names:
        factory = _DIRECT_TOOLS.get(n)
        if factory is None:
            available = sorted(_DIRECT_TOOLS)
            raise KeyError(f"unknown direct tool {n!r}; registered: {available}")
        out.append(factory())
    return out


def list_direct_tool_names() -> list[str]:
    return sorted(_DIRECT_TOOLS)


def build_mainline_tools(
    direct_tool_names: tuple[str, ...] | list[str],
    *,
    mainline_finalize_schema: dict[str, Any] | None = None,
) -> list[Tool]:
    """Assemble mainline's full tool list at run start (then never changes).

    Order: delegate, configure, finalize, direct tools. The cache
    prefix locks once these are sent on the first turn.

    ``mainline_finalize_schema`` overrides the default ``{answer,
    reason}`` shape for configs that need structured mainline output
    (e.g. judge_pair → ``{reasoning, verdict}``). When None, falls
    back to :data:`DEFAULT_FINALIZE_SCHEMA`.
    """
    return [
        build_delegate_tool(),
        build_configure_tool(),
        build_finalize_tool(input_schema=mainline_finalize_schema),
        *resolve_direct_tools(direct_tool_names),
    ]
