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

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from rumil.database import DB
from rumil.tracing.broadcast import Broadcaster

if TYPE_CHECKING:
    from rumil.orchestrators.simple_spine.budget_clock import BudgetClock


def sha8(text: str) -> str:
    """First 8 hex chars of sha256 — used for stable prompt-content hashes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def load_prompt(path: str | Path | None, default: str) -> str:
    """Read a prompt file at ``path``; fall back to ``default`` when path is None.

    Raises if the file is empty/whitespace-only — silent fallback to
    default would let a typo'd path silently swap in the default
    prompt and the variant would fingerprint as the default.
    """
    if path is None:
        return default
    text = Path(path).read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"prompt file is empty or whitespace-only: {path}")
    return text


def splice_assumptions(sys_prompt: str, operating_assumptions: str) -> str:
    """Append an "Operating assumptions" section to a system prompt.

    No-op when ``operating_assumptions`` is empty/whitespace-only. Used
    by the orchestrator (mainline sys_prompt) and by FreeformAgent /
    SampleN (sub-agent sys_prompt) — same shape so the model sees a
    consistent rule-section format wherever assumptions are spliced.
    """
    if not operating_assumptions.strip():
        return sys_prompt
    return (
        sys_prompt.rstrip()
        + "\n\n## Operating assumptions\n\n"
        + operating_assumptions.strip()
        + "\n"
    )


def resolve_spawn_clock(
    parent: BudgetClock,
    *,
    base_cap: int | None,
    override_cap: int | None,
) -> BudgetClock:
    """Pick the BudgetClock a spawn should use given its (optional) sub-cap.

    Returns the parent clock unchanged when no cap is configured. When
    a cap is set (override beats base), carves a child via
    ``parent.carve_child`` so the spawn cannot spend more than the cap;
    tokens still flow up to the parent. Mirrors the clamping pattern in
    NestedOrchSubroutine: a request larger than the parent's remaining
    is clamped down, and ``carve_child`` floors at 1 to keep the call
    well-formed when the parent is already drained (the spawn will then
    immediately report ``tokens_exhausted``).
    """
    cap = override_cap if override_cap is not None else base_cap
    if cap is None:
        return parent
    capped = max(min(int(cap), parent.tokens_remaining), 1)
    return parent.carve_child(capped)


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
        return {
            "model": self.model,
            "sys_prompt_hash": sha8(self.sys_prompt),
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


@dataclass(frozen=True, kw_only=True)
class SubroutineBase:
    """Cross-cutting fields every concrete SubroutineDef shares.

    All four kinds — FreeformAgent, SampleN, NestedOrch, CallType —
    inherit directly from this. Subclasses use
    ``@dataclass(frozen=True, kw_only=True)`` so they can mix required
    and optional fields without ordering pain (and can override a
    base default to require the field, the way NestedOrch does for
    ``base_token_cap``).

    **Field-honoring is per-kind.** Some fields below are honored by
    every kind; others only have effect in some kinds. The base
    declares the surface; each kind documents what it does (or
    doesn't) with each field:

    - ``intent_description`` / ``additional_context_description``:
      honored by every kind in ``spawn_tool_schema`` (override the
      kind-level default schema-field descriptions).
    - ``inherit_assumptions``: honored by FreeformAgent and SampleN
      (spliced into their system prompt at run time); CallType
      honors it by appending an "Operating assumptions" section to
      its staged sub-DB question content; NestedOrch honors it by
      gating whether ``ctx.operating_assumptions`` is forwarded to
      the nested orch's factory.
    - ``base_token_cap``: honored by FreeformAgent and SampleN
      (carve a child BudgetClock); required by NestedOrch; **inert
      on CallType** because the wrapped CallRunner makes LLM calls
      through a path that doesn't tap into the SimpleSpine
      BudgetClock (its budgeting is via init_budget on the staged
      sub-DB instead).
    """

    name: str
    description: str
    overridable: frozenset[str] = field(default_factory=frozenset)
    config_prep: ConfigPrepDef | None = None
    # Optional one-line cost hint shown in the spawn tool description so
    # mainline can plan before its first spawn. Author-supplied because
    # input/output ratios vary too much for a worst-case bound to be
    # useful as auto-computed text.
    cost_hint: str | None = None
    # Per-subroutine overrides for the schema-level field descriptions.
    # The kind-level defaults are intentionally generic ("Short statement
    # of what you want this agent to do") because they have no role
    # context; YAML authors can supply role-specific framing here so
    # mainline knows what shape of text to put in each field for *this*
    # subroutine (e.g. "A side label, 'A' or 'B'" for steelman).
    intent_description: str | None = None
    additional_context_description: str | None = None
    # When True, caller-supplied operating_assumptions (threaded via
    # SpawnCtx) propagate to the spawned subroutine. Default True so
    # global rules (e.g. "judge blind") propagate without per-config
    # wiring; opt out when bias would distort the role (e.g. critics
    # whose job is to push back on assumed framings). See class
    # docstring for how each kind honors this field.
    inherit_assumptions: bool = True
    # Optional per-spawn token cap. See class docstring — different
    # kinds enforce this differently; CallType ignores it entirely.
    base_token_cap: int | None = None

    def apply_assumptions(self, sys_prompt: str, ctx: SpawnCtx) -> str:
        """Splice ctx.operating_assumptions into ``sys_prompt`` when honoring is on.

        Honors ``self.inherit_assumptions`` so kinds that opt out (e.g.
        a critic whose role is to push back on framings) get a clean
        prompt. Returns ``sys_prompt`` unchanged when assumptions are
        empty or inheritance is off.
        """
        if not self.inherit_assumptions:
            return sys_prompt
        return splice_assumptions(sys_prompt, ctx.operating_assumptions)

    def fingerprint(self) -> Mapping[str, Any]:
        """Universal fingerprint contribution.

        Every kind's ``fingerprint()`` should call ``super().fingerprint()``
        and extend with kind-specific fields. The base emits the
        cross-cutting fields (description / overridable / inherit_assumptions /
        base_token_cap / cost_hint / intent_description /
        additional_context_description / config_prep) so editing any of
        them naturally forks the variant fingerprint without per-kind
        bookkeeping.
        """
        out: dict[str, Any] = {
            "name": self.name,
            "description_hash": sha8(self.description),
            "overridable": sorted(self.overridable),
            "inherit_assumptions": self.inherit_assumptions,
            "base_token_cap": self.base_token_cap,
        }
        if self.cost_hint is not None:
            out["cost_hint_hash"] = sha8(self.cost_hint)
        if self.intent_description is not None:
            out["intent_description_hash"] = sha8(self.intent_description)
        if self.additional_context_description is not None:
            out["additional_context_description_hash"] = sha8(self.additional_context_description)
        if self.config_prep is not None:
            out["config_prep"] = self.config_prep.fingerprint()
        return out

    def spawn_tool_schema(self) -> dict[str, Any]:
        """Assemble the JSON schema mainline sees for the spawn tool.

        Base implementation handles the universal ``intent`` /
        ``additional_context`` / ``token_cap`` properties. Kinds with
        their own properties (``max_rounds``, ``n``, etc.) override
        ``_extra_schema_properties()``; kinds with kind-specific
        defaults override ``_default_intent_description()`` /
        ``_default_additional_context_description()`` /
        ``_token_cap_property()``.
        """
        properties = self._build_common_schema_properties()
        properties.update(self._extra_schema_properties())
        return {
            "type": "object",
            "properties": properties,
            "required": ["intent"],
            "additionalProperties": False,
        }

    def _build_common_schema_properties(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "intent": {
                "type": "string",
                "description": self.intent_description or self._default_intent_description(),
            },
        }
        if "additional_context" in self.overridable:
            out["additional_context"] = {
                "type": "string",
                "description": self.additional_context_description
                or self._default_additional_context_description(),
            }
        if "token_cap" in self.overridable and self.base_token_cap is not None:
            out["token_cap"] = self._token_cap_property()
        return out

    def _default_intent_description(self) -> str:
        """Subclass hook: kind-specific default for the ``intent`` schema field."""
        return (
            "Short statement of what you want this subroutine to do. "
            "Substituted into the user prompt template as {intent}."
        )

    def _default_additional_context_description(self) -> str:
        """Subclass hook: kind-specific default for ``additional_context``."""
        return (
            "Extra context / scratchpad excerpts to splice into the "
            "user prompt under {additional_context}."
        )

    def _token_cap_property(self) -> dict[str, Any]:
        """Subclass hook: kind-specific ``token_cap`` schema property."""
        return {
            "type": "integer",
            "minimum": 500,
            "description": (
                f"Per-spawn token sub-cap (default {self.base_token_cap}). "
                "Tokens still debit the parent budget; capped at the "
                "parent's remaining."
            ),
        }

    def _extra_schema_properties(self) -> dict[str, Any]:
        """Subclass hook: properties beyond the universal triplet.

        Default returns an empty dict. Subclasses with kind-specific
        spawn-tool fields (``max_rounds``, ``n``, etc.) override this.
        """
        return {}


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
