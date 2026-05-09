"""Base types for SimpleSpine subroutines.

A ``SubroutineDef`` is a named, frozen spec of a thing the mainline
agent can spawn. The protocol is intentionally narrow:

- ``spawn_tool_schema()`` describes the tool exposed to mainline.
- ``run(ctx, overrides)`` executes the subroutine and returns a
  ``SubroutineResult`` whose ``text_summary`` is what bubbles back to
  mainline as a tool result.
- ``fingerprint()`` returns a stable dict for the orch fingerprint.
- ``config_prep`` is an optional hidden second-stage LLM call that
  branches off mainline (same system + history) to elaborate a thin
  spawn payload into the full subroutine config — see
  :class:`ConfigPrepDef`.

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
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel

from rumil.database import DB
from rumil.tracing.broadcast import Broadcaster

if TYPE_CHECKING:
    from rumil.orchestrators.simple_spine.artifacts import ArtifactStore
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
    config-prep LLM call elaborates that intent into the subroutine's
    full config. The elaborated config (matching ``output_schema``) is
    what the subroutine actually sees on ``ctx.prepped_config``.

    The prep call **inherits mainline's full context**: same system
    prompt, same message history up through (and including) the
    assistant turn that issued the spawn ``tool_use``. Per the
    Anthropic Messages API, that trailing assistant turn must be
    followed by a user turn containing a ``tool_result`` for every
    ``tool_use_id``; the orchestrator synthesizes placeholder
    ``tool_result`` blocks (the spawn hasn't actually run) and appends
    a text instruction asking the model to elaborate the config as
    structured output. Sibling parallel ``tool_use`` blocks in that
    trailing turn — other spawns, ``finalize``, etc. — get
    "deferred for branched elaboration" placeholders so the prep call
    is well-formed without modifying mainline's actual flow.

    Cache implications: mainline's system block reads from cache; the
    prep call deliberately omits mainline's tool definitions (tool
    definitions sit between system and messages in the cache key, and
    we're branching to structured output not a tool call) so the
    messages-prefix cache won't hit, but the system block still does.

    ``output_schema`` is the structured shape the prep call must
    return; each subroutine kind interprets it (e.g. FreeformAgent
    expects a schema with ``sys_prompt``, ``user_prompt``,
    ``enabled_tools``, ``max_rounds`` fields).

    ``instructions`` is appended to the synthetic elaboration text
    that the orchestrator writes after the synthetic tool_results —
    use it to nudge the elaborator with kind-specific framing
    ("Pick a sys_prompt that matches the intent's role", etc.).
    Optional; default empty.
    """

    model: str
    output_schema: type[BaseModel]
    instructions: str = ""

    def fingerprint(self) -> Mapping[str, Any]:
        out: dict[str, Any] = {
            "model": self.model,
            "output_schema": self.output_schema.__name__,
        }
        if self.instructions:
            out["instructions_hash"] = sha8(self.instructions)
        return out


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
    # Shared artifact store threaded through the run. Subroutines splice
    # entries via ``SubroutineBase.consumes`` (static, declared) and
    # mainline-supplied ``include_artifacts`` (dynamic, per-spawn). None
    # in unit-test paths that don't exercise the artifact channel; the
    # FreeformAgent / SampleN run loops fall back to no-op when None.
    artifacts: ArtifactStore | None = None
    # Mainline-chosen artifact keys to include in this spawn's user
    # prompt, on top of the subroutine's static ``consumes``. Validated
    # by the orchestrator before this ``SpawnCtx`` is built — invalid
    # keys never reach a subroutine's ``run``.
    include_artifacts: tuple[str, ...] = field(default_factory=tuple)


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
      (carve a child BudgetClock); required by NestedOrch;
      **rejected by CallType** in ``__post_init__`` because the
      wrapped CallRunner makes LLM calls through a path that doesn't
      tap into the SimpleSpine BudgetClock (its budgeting is via
      init_budget on the staged sub-DB instead) — silent acceptance
      would let YAML authors set a value that does nothing.
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
    # Static artifact keys the subroutine always wants spliced into
    # its user prompt. Resolved against the run's ArtifactStore at
    # spawn time; missing keys raise loudly (a typo would silently
    # produce thin context). FreeformAgent and SampleN honor this;
    # CallType and NestedOrch raise in __post_init__ if non-empty
    # because their LLM-call paths don't go through the spine's
    # spawn-prompt rendering — artifact integration there needs a
    # separate design and is out of MVP scope.
    consumes: tuple[str, ...] = ()

    def carve_spawn_clock(
        self,
        parent: BudgetClock,
        *,
        override_cap: int | None,
    ) -> BudgetClock:
        """Per-spawn :class:`BudgetClock` for accounting + cap enforcement.

        The orchestrator calls this once per spawn before invoking
        ``run`` and threads the returned clock through ``ctx.budget_clock``
        — that means subroutine kinds always see a clock scoped to their
        own spawn, and the trace's ``tokens_consumed`` reads directly from
        ``spawn_clock.tokens_used`` (no parent-clock-delta needed, which
        would double-count under parallel spawns).

        Default behavior: carve a child via :func:`resolve_spawn_clock`
        using ``base_token_cap`` (or the override). When neither is set
        we still carve a child clock (capped at the parent's remaining)
        so ``tokens_used`` is per-spawn accurate even without a configured
        cap. Overridden by :class:`CallType` whose LLM calls bypass the
        SimpleSpine clock — it returns the parent unchanged so the
        existing parent-delta accounting in the orchestrator still works
        for that one kind.
        """
        cap = override_cap if override_cap is not None else self.base_token_cap
        if cap is None:
            cap = parent.tokens_remaining
        capped = max(min(int(cap), parent.tokens_remaining), 1)
        return parent.carve_child(capped)

    def render_artifact_block(self, ctx: SpawnCtx) -> str:
        """Build the artifact block to prepend to the spawn's user prompt.

        Combines static ``consumes`` (declared on this subroutine) with
        mainline-supplied ``include_artifacts`` (dynamic, per-spawn) — order
        preserved, duplicates removed. Returns empty string when nothing
        applies (no consumes, no include, or no store).

        The orchestrator validates ``include_artifacts`` keys before this
        runs (invalid keys land an ``is_error`` tool_result instead of
        executing the spawn), but ``consumes`` keys are validated here at
        spawn time — a typo'd YAML config raises rather than silently
        producing thin context.
        """
        if ctx.artifacts is None:
            if self.consumes:
                raise ValueError(
                    f"subroutine {self.name!r} declares consumes={list(self.consumes)} "
                    "but ctx.artifacts is None — orchestrator must thread an "
                    "ArtifactStore through SpawnCtx for kinds that consume artifacts"
                )
            return ""
        keys: list[str] = []
        seen: set[str] = set()
        for k in (*self.consumes, *ctx.include_artifacts):
            if k in seen:
                continue
            seen.add(k)
            keys.append(k)
        if not keys:
            return ""
        missing = ctx.artifacts.require_keys(self.consumes)
        if missing:
            raise ValueError(
                f"subroutine {self.name!r} declares consumes={list(self.consumes)} "
                f"but the run's ArtifactStore lacks key(s): {missing}. "
                "Caller must seed these via OrchInputs.artifacts, or an "
                "earlier subroutine must produce them."
            )
        return ctx.artifacts.render_block(keys)

    def apply_assumptions(self, text: str, ctx: SpawnCtx) -> str:
        """Splice ctx.operating_assumptions into ``text`` when honoring is on.

        Single entry point for subroutine kinds — handles the
        ``inherit_assumptions`` gate and the format-uniformity contract.
        Used for sys prompts (FreeformAgent, SampleN) and question
        content (CallType writing into its staged sub-DB). Returns
        ``text`` unchanged when assumptions are empty or inheritance
        is off. Don't call ``splice_assumptions`` directly from a
        subroutine — bypassing this method skips the gate.
        """
        if not self.inherit_assumptions:
            return text
        return splice_assumptions(text, ctx.operating_assumptions)

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
            "consumes": list(self.consumes),
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
        required = ["intent", *self._extra_required_fields()]
        return {
            "type": "object",
            "properties": properties,
            "required": required,
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
        # ``include_artifacts`` is always available on FreeformAgent /
        # SampleN spawn schemas — it's the dynamic counterpart to the
        # static ``consumes`` declaration. Mainline picks earlier-spawned
        # artifact keys (announced in tool_result messages as they're
        # produced) to splice into this spawn's user prompt. Empty list
        # = nothing dynamic; static ``consumes`` still applies.
        if self._supports_include_artifacts():
            out["include_artifacts"] = {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of artifact keys to splice into this "
                    "spawn's user prompt under '## Artifacts'. Pick from "
                    "keys announced in earlier tool_result messages "
                    "(format: `<sub_name>/<spawn_id_short>` or "
                    "`<name>/<spawn_id>/<sub_key>`) plus any input-seeded "
                    "keys announced at run start. The subroutine's "
                    "static `consumes` declaration is always spliced "
                    "regardless — this field is purely additive."
                ),
            }
        return out

    def _supports_include_artifacts(self) -> bool:
        """Override in CallType / NestedOrch kinds where artifact splicing isn't wired."""
        return True

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

    def _extra_required_fields(self) -> list[str]:
        """Subclass hook: extra required fields beyond ``intent``.

        Default returns an empty list. Subclasses with required
        kind-specific properties (e.g. nested_orch's ``question_headline``)
        override this. Returned names must appear in
        ``_extra_schema_properties()`` keys.
        """
        return []


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

    ``produces`` is folded into the run's :class:`ArtifactStore` after
    the spawn returns. The empty-key entry ``{"": text}`` is keyed as
    ``<sub_name>/<spawn_id_short>``; non-empty sub-keys become
    ``<sub_name>/<spawn_id_short>/<sub_key>``. Multi-output spawns
    (e.g. SampleN producing per-sample) use distinct sub-keys.
    """

    text_summary: str
    tokens_used: int = 0
    extra: Mapping[str, Any] = field(default_factory=dict)
    produces: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class SubroutineDef(Protocol):
    """A spawnable thing in the SimpleSpine library."""

    name: str
    description: str
    overridable: frozenset[str]
    config_prep: ConfigPrepDef | None
    consumes: tuple[str, ...]

    def spawn_tool_schema(self) -> dict[str, Any]:
        """Return the JSON schema for the spawn tool's ``input_schema``.

        The schema's properties must be a subset of ``overridable`` plus
        any always-required orchestration fields (e.g. ``intent`` when
        ``config_prep`` is set). Generated, not hand-written, so the
        ``overridable`` whitelist stays the single source of truth.
        """
        ...

    def fingerprint(self) -> Mapping[str, Any]: ...

    def carve_spawn_clock(
        self, parent: BudgetClock, *, override_cap: int | None
    ) -> BudgetClock: ...

    async def run(self, ctx: SpawnCtx, overrides: Mapping[str, Any]) -> SubroutineResult: ...
