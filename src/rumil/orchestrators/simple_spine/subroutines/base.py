"""Base types for SimpleSpine subroutines.

A ``SubroutineDef`` is a named, frozen spec of a thing the mainline
agent can spawn. The protocol is intentionally narrow:

- ``spawn_tool_schema()`` describes the tool exposed to mainline.
- ``run(ctx, overrides)`` executes the subroutine and returns a
  ``SubroutineResult`` whose ``text_summary`` is what bubbles back to
  mainline as a tool result.
- ``fingerprint()`` returns a stable dict for the orch fingerprint.
- ``config_prep`` is an optional hidden second-stage LLM call that
  elaborates a thin spawn payload into the full subroutine config —
  see :class:`ConfigPrepDef`.

Concurrency: every subroutine's ``run`` is awaited concurrently with
its peers within one mainline turn (``asyncio.gather``). Subroutines
that themselves spawn LLM calls should use the runtime semaphore
(handled inside ``rumil.llm.call_anthropic_api``) — no extra plumbing
needed at this layer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from rumil.database import DB
from rumil.tracing.broadcast import Broadcaster

if TYPE_CHECKING:
    from rumil.orchestrators.simple_spine.budget_clock import BudgetClock


@dataclass(frozen=True)
class ConfigPrepDef:
    """Hidden second-stage LLM call that elaborates a thin spawn intent.

    When set on a SubroutineDef, the spawn tool exposes a small
    ``intent`` payload to mainline; before running the subroutine, a
    config-prep LLM call converts that intent (plus optional slices of
    mainline's persistent thread) into the subroutine's full config. The
    elaborated config is what the subroutine actually sees.

    ``output_schema`` is the structured shape the prep call must return;
    each subroutine kind interprets it (e.g. FreeformAgent expects a
    schema with ``sys_prompt``, ``user_prompt``, ``tools``,
    ``additional_context`` fields).
    """

    model: str
    sys_prompt: str
    output_schema: type[BaseModel]
    mainline_context: Literal["none", "last_turn", "last_k_turns"] = "last_turn"
    last_k: int = 2

    def fingerprint(self) -> Mapping[str, Any]:
        from rumil.orchestrators.simple_spine.config import _sha8

        return {
            "model": self.model,
            "sys_prompt_hash": _sha8(self.sys_prompt),
            "output_schema": self.output_schema.__name__,
            "mainline_context": self.mainline_context,
            "last_k": self.last_k,
        }


@dataclass
class SpawnCtx:
    """Per-spawn execution context handed to a subroutine's ``run``.

    Carries the parent orch's DB / budget / tracing surface plus the
    optional config-prep elaborated config and a slice of mainline's
    thread (when the subroutine declared one).
    """

    db: DB
    budget_clock: BudgetClock
    broadcaster: Broadcaster | None
    parent_call_id: str
    question_id: str
    spawn_id: str
    # Slice of mainline's thread the subroutine is allowed to see. Empty
    # when the SubroutineDef did not request mainline context.
    mainline_messages: Sequence[Mapping[str, Any]] = field(default_factory=list)
    # Output of the config-prep LLM call (when ``config_prep`` is set);
    # subroutine implementations cast this to their expected schema.
    prepped_config: BaseModel | None = None
    # Caller-supplied operating assumptions threaded from OrchInputs.
    # Subroutines that opt in via ``inherit_assumptions`` append this to
    # their own system prompt at run time.
    operating_assumptions: str = ""


@dataclass
class SubroutineResult:
    """What a spawned subroutine returns to mainline.

    ``text_summary`` is what is fed back as the tool-result message body —
    this is the only surface the mainline agent sees. Keep it small and
    informative; large blobs blow up the persistent thread fast.

    ``tokens_used`` is the *additional* tokens this subroutine consumed
    beyond what the BudgetClock has already recorded. Subroutines that
    use ``rumil.llm`` text/structured-call helpers should pass the
    BudgetClock through and leave this at 0; subroutines that bypass
    helpers must report their own count here.
    """

    text_summary: str
    tokens_used: int = 0
    extra: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class SubroutineDef(Protocol):
    """A spawnable thing in the SimpleSpine library."""

    name: str
    description: str
    overridable: frozenset[str]
    config_prep: ConfigPrepDef | None

    def spawn_tool_schema(self) -> dict[str, Any]:
        """Return the JSON schema for the spawn tool's ``input_schema``.

        The schema's properties must be a subset of ``overridable`` plus
        any always-required orchestration fields (e.g. ``intent`` when
        ``config_prep`` is set). Generated, not hand-written, so the
        ``overridable`` whitelist stays the single source of truth.
        """
        ...

    def fingerprint(self) -> Mapping[str, Any]: ...

    async def run(self, ctx: SpawnCtx, overrides: Mapping[str, Any]) -> SubroutineResult: ...
