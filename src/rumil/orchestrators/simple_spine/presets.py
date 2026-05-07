"""Named SimpleSpineConfig presets.

The registry is the canonical source of named configs until the YAML
loader (task 8) lands. Add a new variant by defining a builder function
and registering it via :func:`register_preset` at module import time.

Two starter variants ship out of the box:

- ``"essay_continuation"`` — for versus completion runs. Library
  includes a single drafter (FreeformAgent) and a sample-N critic
  ensemble. Output guidance points the agent at the
  ``<continuation>...</continuation>`` envelope CompleteEssayTask
  expects in its extracted artifact.
- ``"judge_pair"`` — for versus judging runs. Library includes a
  reader, a sample-N steelman ensemble, and a verdict subroutine.
  Output guidance pins the 7-point preference label format
  ``extract_preference`` parses.

These are intentionally minimal — first-pass shapes for the user to
iterate against. Forking by editing copies in YAML or registering new
named builders is the expected workflow.
"""

from __future__ import annotations

from collections.abc import Callable

from rumil.orchestrators.simple_spine.config import (
    _DEFAULT_MAIN_SYSTEM_PROMPT,
    SimpleSpineConfig,
)
from rumil.orchestrators.simple_spine.subroutines import (
    FreeformAgentSubroutine,
    SampleNSubroutine,
    SubroutineDef,
)

PresetBuilder = Callable[[], SimpleSpineConfig]
_REGISTRY: dict[str, PresetBuilder] = {}


def register_preset(name: str, builder: PresetBuilder) -> None:
    """Register a SimpleSpineConfig builder under ``name``.

    Idempotent: re-registering the same name silently overwrites — this
    keeps test fixtures and module-level register calls safe to re-run.
    """
    _REGISTRY[name] = builder


def get_preset(name: str) -> SimpleSpineConfig:
    """Look up and instantiate a SimpleSpineConfig by name."""
    if name not in _REGISTRY:
        known = sorted(_REGISTRY)
        raise KeyError(f"unknown SimpleSpine preset {name!r}; registered: {known}")
    return _REGISTRY[name]()


def list_presets() -> list[str]:
    return sorted(_REGISTRY)


_DRAFTER_SYS = (
    "You are an essay continuation drafter. The user message contains "
    "the prefix of an essay and a target length. Produce a substantive "
    "continuation that picks up the argumentative thread, matches the "
    "voice and register, and stays at-or-under the target length. "
    "Wrap your final continuation in <continuation>...</continuation> "
    "tags; only content inside the tags is kept."
)

_DRAFTER_USER = (
    "## Intent\n{intent}\n\n"
    "## Additional context\n{additional_context}\n\n"
    "Produce the continuation now."
)

_CRITIC_SYS = (
    "You are reviewing a draft essay continuation against the prefix. "
    "Identify problems specifically — weak arguments, factual errors, "
    "voice mismatches, overlong sections, missed opportunities. Be "
    "concrete and quote phrases when relevant. Do not write the next "
    "draft — surface what's wrong so the editor can act."
)

_CRITIC_USER = (
    "## Intent\n{intent}\n\n## Draft + prefix\n{additional_context}\n\nProduce your critique now."
)

_READER_SYS = (
    "You are the READ stage of a structured pairwise judgment. The user "
    "message contains the dimension rubric, the essay prefix, and the "
    "two continuations (A and B). Produce a careful initial reading: "
    "name what each continuation is doing, identify divergence points, "
    "score against the rubric with concrete evidence. Do NOT emit a "
    "preference label — that comes from the verdict stage."
)

_READER_USER = (
    "## Intent\n{intent}\n\n## Pair + rubric\n{additional_context}\n\nProduce the initial read now."
)

_STEELMAN_SYS = (
    "You are steelmanning one side of a pairwise judgment against a "
    "rubric. Produce the strongest honest case for the side the user "
    "names. Do not hedge. Do not concede the other side's points "
    "preemptively. The downstream verdict stage will weigh your case "
    "against the read."
)

_STEELMAN_USER = (
    "## Intent (which side to steelman)\n{intent}\n\n"
    "## Pair + rubric + prior read\n{additional_context}\n\n"
    "Produce the steelman now."
)

_VERDICT_SYS = (
    "You are the VERDICT stage of a structured pairwise judgment. "
    "Synthesize a final verdict on which continuation better satisfies "
    "the dimension's rubric, weighing the prior read and any steelmans. "
    "Write 2-5 paragraphs of reasoning, then end with exactly one of "
    "these labels on its own line, nothing else after it:\n"
    "  A strongly preferred\n"
    "  A somewhat preferred\n"
    "  A slightly preferred\n"
    "  Approximately indifferent between A and B\n"
    "  B slightly preferred\n"
    "  B somewhat preferred\n"
    "  B strongly preferred\n"
    "The downstream harness extracts the label by string match — copy "
    "verbatim, no quotes, no surrounding text on the same line."
)

_VERDICT_USER = (
    "## Intent\n{intent}\n\n"
    "## Pair + rubric + prior stages\n{additional_context}\n\n"
    "Produce the verdict and the 7-point label now."
)


def _build_essay_continuation() -> SimpleSpineConfig:
    """Default essay-continuation preset.

    Library: drafter (single FreeformAgent), critic (SampleN with n=3).
    The mainline agent decides when to draft vs. critique vs. revise vs.
    finalize. Models default to claude-opus-4-7; override via settings'
    ``rumil_model_override`` (which run_versus sets from --model).
    """
    drafter = FreeformAgentSubroutine(
        name="draft",
        description=(
            "Draft an essay continuation. The drafter sees the prefix "
            "and target length via the question; pass any additional "
            "context (revision targets, planning notes) under "
            "additional_context. Returns the continuation wrapped in "
            "<continuation> tags."
        ),
        sys_prompt=_DRAFTER_SYS,
        user_prompt_template=_DRAFTER_USER,
        model="claude-opus-4-7",
        max_rounds=1,
        # Wide enough that even verbose / planning-heavy drafters can
        # finish a full continuation + closing </continuation> tag
        # without truncation. Sonnet/Opus 4.x both support 32k+ output.
        max_tokens=32_000,
    )
    critic = SampleNSubroutine(
        name="critique",
        description=(
            "Run N independent critics on a draft to surface diverse "
            "criticism. Pass the draft + prefix as additional_context."
        ),
        sys_prompt=_CRITIC_SYS,
        user_prompt_template=_CRITIC_USER,
        model="claude-sonnet-4-6",
        n=3,
        temperature=1.0,
        max_tokens=2048,
    )
    library: tuple[SubroutineDef, ...] = (drafter, critic)  # type: ignore[assignment]
    return SimpleSpineConfig(
        main_model="claude-opus-4-7",
        process_library=library,
        max_parallel_spawns_per_turn=4,
    )


_JUDGE_PAIR_MAIN_SYSTEM_PROMPT = (
    _DEFAULT_MAIN_SYSTEM_PROMPT + "\n\n"
    "## Wire-format constraint for pairwise judging\n\n"
    "The harness extracts the pair's preference from your `finalize` "
    "answer by exact-string match against these seven labels:\n"
    "  - A strongly preferred\n"
    "  - A somewhat preferred\n"
    "  - A slightly preferred\n"
    "  - Approximately indifferent between A and B\n"
    "  - B slightly preferred\n"
    "  - B somewhat preferred\n"
    "  - B strongly preferred\n\n"
    "Your `finalize.answer` MUST end with one of these seven labels, "
    "verbatim, on its own line, with nothing after it. The verdict "
    "subroutine will produce a label that satisfies this constraint; "
    "preserve it unchanged in the final answer. Do NOT substitute a "
    "different phrasing (`A clearly better`, `Preference: -2`, "
    "`A is the winner`, etc.) — those will not parse and the judgment "
    "will be discarded. If you genuinely disagree with the verdict's "
    "strength, your only acceptable move is to spawn the verdict "
    "subroutine again with a tightening intent and use its new label."
)


def _build_judge_pair() -> SimpleSpineConfig:
    """Default pair-judging preset.

    Library: reader (FreeformAgent), steelman (SampleN n=2 — one each
    side), verdict (FreeformAgent). The mainline agent stages the
    pipeline and calls finalize once verdict has emitted a label.

    The mainline system prompt extends the default with an explicit
    wire-format constraint listing the seven canonical labels — without
    this, mainline opus rewrites the verdict's correct label into
    non-canonical phrasing on borderline pairs, breaking parse.
    """
    reader = FreeformAgentSubroutine(
        name="read",
        description=(
            "Initial reading of the pair against the rubric. Passes the "
            "prefix + both continuations + rubric as additional_context."
        ),
        sys_prompt=_READER_SYS,
        user_prompt_template=_READER_USER,
        model="claude-opus-4-7",
        max_rounds=1,
        max_tokens=4096,
    )
    steelman = SampleNSubroutine(
        name="steelman",
        description=(
            "Steelman one side of the pair. Pass the side ('A' / 'B') "
            "as intent and the pair + rubric + prior read as "
            "additional_context. Returns N independent steelmans."
        ),
        sys_prompt=_STEELMAN_SYS,
        user_prompt_template=_STEELMAN_USER,
        model="claude-sonnet-4-6",
        n=2,
        temperature=1.0,
        max_tokens=2048,
    )
    from rumil.versus_prompts import extract_preference

    def _has_canonical_label(text: str) -> bool:
        return extract_preference(text) is not None

    verdict_retry_message = (
        "Your prior response did not end with one of the seven canonical "
        "preference labels. The downstream harness extracts the verdict by "
        "exact-string match against:\n"
        "  - A strongly preferred\n"
        "  - A somewhat preferred\n"
        "  - A slightly preferred\n"
        "  - Approximately indifferent between A and B\n"
        "  - B slightly preferred\n"
        "  - B somewhat preferred\n"
        "  - B strongly preferred\n\n"
        "Do not use numeric scales (`Preference: -2`), do not invent new "
        "phrasing (`A clearly better`, `A is the winner`), do not wrap the "
        "label in quotes or punctuation. Re-emit your verdict reasoning "
        "and end with the single closest label from the list above on its "
        "own line, with nothing after it."
    )
    verdict = FreeformAgentSubroutine(
        name="verdict",
        description=(
            "Synthesize the final verdict + 7-point preference label. "
            "Pass the pair + rubric + prior stages as additional_context. "
            "Returns the verdict text ending with the label on its own line."
        ),
        sys_prompt=_VERDICT_SYS,
        user_prompt_template=_VERDICT_USER,
        model="claude-opus-4-7",
        max_rounds=1,
        max_tokens=4096,
        # Retry if the verdict response doesn't end with a canonical label.
        # Defends against the angels b=4 failure mode where opus emitted
        # `Preference: -2` despite the 7-pt block being in its sys prompt.
        response_validator=_has_canonical_label,
        response_validator_name="extract_preference_not_none",
        retry_message=verdict_retry_message,
        response_max_retries=2,
    )
    library: tuple[SubroutineDef, ...] = (reader, steelman, verdict)  # type: ignore[assignment]
    return SimpleSpineConfig(
        main_model="claude-opus-4-7",
        main_system_prompt=_JUDGE_PAIR_MAIN_SYSTEM_PROMPT,
        process_library=library,
        max_parallel_spawns_per_turn=3,
    )


register_preset("essay_continuation", _build_essay_continuation)
register_preset("judge_pair", _build_judge_pair)
register_preset("default", _build_essay_continuation)
