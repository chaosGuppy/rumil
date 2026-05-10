"""Structured types axon uses internally and exposes to mainline.

The load-bearing type is :class:`DelegateConfig` — what configure
emits as a tool call to fully specify a delegate's inner loop. The
mainline ``delegate`` tool surface is intentionally tiny (intent +
inherit_context + budget_usd + n); configure does the heavy lifting.

:class:`FinalizeSchemaSpec` is how DelegateConfig points at the
expected finalize-tool input_schema — either a registered name (cached,
shared across delegates) or an inline JSON Schema (one-off custom).
The orchestrator builds the actual finalize tool def per delegate using
this spec.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class FinalizeSchemaSpec(BaseModel):
    """How to resolve the finalize tool's input_schema for a delegate.

    Exactly one of ``ref`` or ``inline`` must be set. ``ref`` is a key
    into the run's ``finalize_schema_registry`` (cache-friendly across
    delegates that reuse the same schema). ``inline`` is a literal JSON
    Schema dict for one-off shapes.
    """

    ref: str | None = None
    inline: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> FinalizeSchemaSpec:
        if (self.ref is None) == (self.inline is None):
            raise ValueError("FinalizeSchemaSpec: exactly one of `ref` or `inline` must be set")
        return self


class SystemPromptSpec(BaseModel):
    """How to resolve the inner loop's system prompt for an isolation delegate.

    Exactly one of ``ref`` or ``inline`` must be set. For continuation
    delegates this should be ``None`` entirely — the inner loop reuses
    the spine's system unchanged.
    """

    ref: str | None = None
    inline: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> SystemPromptSpec:
        if (self.ref is None) == (self.inline is None):
            raise ValueError("SystemPromptSpec: exactly one of `ref` or `inline` must be set")
        return self


SideEffect = Literal["write_artifact"]


class DelegateConfig(BaseModel):
    """The structured object configure emits per delegate.

    Captures everything the inner loop needs beyond what mainline
    already supplied via the delegate call (intent, inherit_context,
    budget_usd, n).

    The continuation/isolation coupling rule is enforced at validation
    time: when ``inherit_context=True`` (passed in via the parent
    delegate call, not stored here), ``system_prompt`` MUST be ``None``
    and ``tools`` MUST be ``None`` (meaning "use the spine's
    full set"). The orchestrator threads inherit_context in alongside
    this config and validates the combination.
    """

    # Inner-loop setup
    system_prompt: SystemPromptSpec | None = Field(
        default=None,
        description=(
            "Inner-loop system prompt. Set to None to inherit the spine's "
            "system (required when inherit_context=True; allowed only "
            "when inherit_context=False if you want spine's system on a "
            "fresh-start delegate)."
        ),
    )
    tools: list[str] | None = Field(
        default=None,
        description=(
            "Names of tools (from the run's tool registry) the inner "
            "loop can call. Set to None to inherit the spine's full tool "
            "set (required when inherit_context=True). Always-present: "
            "the universal `finalize` tool, configured via "
            "finalize_schema."
        ),
    )

    # Termination
    max_rounds: int = Field(
        ...,
        ge=1,
        description="Hard cap on inner-loop assistant turns.",
    )
    finalize_schema: FinalizeSchemaSpec = Field(
        ...,
        description=(
            "Shape of the inner loop's finalize tool input. The result "
            "comes back to mainline as the delegate's tool_result."
        ),
    )

    # Side effects
    side_effects: list[SideEffect] = Field(
        default_factory=list,
        description=(
            "Persistence beyond returning the result to mainline. "
            "`write_artifact` requires `artifact_key` to be set. "
            "Workspace page creation happens inside the delegate via "
            "the `create_page` tool, not as a side effect."
        ),
    )
    artifact_key: str | None = Field(
        default=None,
        description=(
            "Required when 'write_artifact' is in side_effects. For n>1 "
            "delegates, individual sample keys are derived as "
            "<artifact_key>/<sample_idx>."
        ),
    )

    # Context handoff (orthogonal to inherit_context — just extra prose
    # to splice into the inner loop's framing user message)
    extra_context: str | None = Field(
        default=None,
        description=(
            "Optional extra prose for the inner loop's framing user "
            "message. Use to reference artifacts by key, or to give "
            "context not in the spine that the inner agent needs."
        ),
    )

    # Audit trail
    rationale: str = Field(
        ...,
        description=(
            "Brief reasoning for these config choices. Surfaced in the "
            "trace; not consumed by the inner loop."
        ),
    )

    @model_validator(mode="after")
    def _side_effects_consistency(self) -> DelegateConfig:
        if "write_artifact" in self.side_effects and self.artifact_key is None:
            raise ValueError(
                "DelegateConfig: side_effects includes 'write_artifact' but artifact_key is None"
            )
        if "write_artifact" not in self.side_effects and self.artifact_key is not None:
            raise ValueError(
                "DelegateConfig: artifact_key is set but 'write_artifact' is not in side_effects"
            )
        return self


class DelegateRequest(BaseModel):
    """What mainline emits via the delegate tool — the thin call.

    Surface intentionally minimal: configure fills in everything else.
    """

    intent: str = Field(..., description="Plain-language description of the sub-task.")
    inherit_context: bool = Field(
        ...,
        description=(
            "True ⇒ inner loop inherits spine messages + tools + system "
            "(cache-shared continuation). False ⇒ fresh start; configure "
            "may pick any system / tools."
        ),
    )
    budget_usd: float = Field(
        ...,
        gt=0,
        description=(
            "Mainline-set commitment of cost out of the run's remaining "
            "budget. Configure cannot override."
        ),
    )
    n: int = Field(
        default=1,
        ge=1,
        description=(
            "Number of independent samples to run with the same config. "
            "configure runs once; n inner loops run in parallel; results "
            "come back to mainline as a list."
        ),
    )
