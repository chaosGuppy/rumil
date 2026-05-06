"""Granular prompt model: sections within a file, parts composed per call.

A system prompt sent to the model is a *composition*, not a single file.
``rumil.llm.build_system_prompt`` joins:

- ``preamble.md`` (with ``{{TASK}}`` substituted)
- ``{call_type}.md`` (per-call instructions)
- ``citations.md`` (when the call creates content-bearing pages)
- ``grounding.md`` (always, when preamble is included)
- (rarely) ``scout_budget_awareness_experimental.md``

Some calls bypass parts of this — ``include_preamble=False`` for
``generate_artefact`` / ``critique_artefact_request_only``;
``include_per_call=False`` for the prioritization variants where the
per-call file is rendered into the user message instead. Atlas mirrors
all of that as a ``PromptComposition`` for each ``CallType``.

Within a single file, ## headers split the content into named
``PromptSection``s — useful for cross-prompt navigation, anchored
linking, and surfacing "which calls transitively include this section"
through their compositions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rumil.atlas.schemas import (
    PromptComposition,
    PromptPart,
    PromptSection,
)
from rumil.models import CallType
from rumil.prompts import PROMPTS_DIR

_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _slugify(title: str) -> str:
    """Match the slug a markdown renderer would generate for this header."""
    s = title.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def parse_prompt_sections(content: str) -> list[PromptSection]:
    """Split a markdown prompt by ## (or deeper) headers into sections.

    Content before any ## header is returned as a synthetic ``"(intro)"``
    section so nothing is dropped. Sections of equal or higher depth than
    the most recent open section close it; otherwise headers are folded
    into the parent section's body verbatim — the structure surfaces
    only top-level top-level grouping (## headers), not recursive
    subsection trees, because that's the level at which rumil prompts
    actually carry meaning.
    """
    matches: list[tuple[int, str, int]] = []
    for m in _HEADER_RE.finditer(content):
        hashes = m.group(1)
        if len(hashes) != 2:
            continue
        matches.append((m.start(), m.group(2), m.end()))

    sections: list[PromptSection] = []

    if not matches:
        body = content.strip()
        if not body:
            return []
        return [
            PromptSection(
                title="(intro)",
                level=0,
                anchor="intro",
                body=body,
                char_count=len(body),
            )
        ]

    intro = content[: matches[0][0]].strip()
    if intro:
        sections.append(
            PromptSection(
                title="(intro)",
                level=0,
                anchor="intro",
                body=intro,
                char_count=len(intro),
            )
        )

    for i, (_start, title, body_start) in enumerate(matches):
        end = matches[i + 1][0] if i + 1 < len(matches) else len(content)
        body = content[body_start:end].strip()
        sections.append(
            PromptSection(
                title=title.strip(),
                level=2,
                anchor=_slugify(title),
                body=body,
                char_count=len(body),
            )
        )

    return sections


def get_prompt_sections(name: str) -> list[PromptSection]:
    if not name.endswith(".md"):
        name = f"{name}.md"
    path = PROMPTS_DIR / name
    if not path.exists():
        return []
    return parse_prompt_sections(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class _PartSpec:
    """Internal: a part's identity, how it joins the composition, and any
    condition under which it's included."""

    file: str
    role: str
    location: str = "system"
    condition: str | None = None
    optional: bool = False


_DEFAULT_PARTS: tuple[_PartSpec, ...] = (
    _PartSpec(file="preamble.md", role="preamble"),
    _PartSpec(
        file="<call_type>.md",
        role="per_call",
    ),
    _PartSpec(
        file="citations.md",
        role="citations",
        condition="creates content-bearing pages",
        optional=True,
    ),
    _PartSpec(file="grounding.md", role="grounding"),
    _PartSpec(
        file="scout_budget_awareness_experimental.md",
        role="extra",
        condition="experimental scout budget contextvar set",
        optional=True,
    ),
)


# Per-call composition overrides. Mirrors the actual flag-paths in
# ``build_system_prompt`` callers — see ``src/rumil/calls/``,
# ``src/rumil/orchestrators/``.
#
# Keys are call types (or pseudo-call-types like the prioritization
# variants that don't have their own CallType enum value but do have
# distinct prompt files). Values describe what's *different* from
# _DEFAULT_PARTS for that key.
_OVERRIDES: dict[str, list[_PartSpec]] = {
    # Generative workflow: domain-neutral writer, no rumil framing.
    CallType.GENERATE_ARTEFACT.value: [
        _PartSpec(file="<call_type>.md", role="per_call"),
    ],
    CallType.CRITIQUE_ARTEFACT_REQUEST_ONLY.value: [
        _PartSpec(file="<call_type>.md", role="per_call"),
    ],
    # Prioritization variants: per-call instructions live in the user
    # message; system prompt is preamble + grounding only (no citations
    # because no page creation happens).
    "two_phase_initial_prioritization": [
        _PartSpec(file="preamble.md", role="preamble"),
        _PartSpec(
            file="two_phase_initial_prioritization.md",
            role="per_call",
            location="user",
            condition="rendered into user message via build_user_message(call_type=...)",
        ),
        _PartSpec(file="grounding.md", role="grounding"),
    ],
    "two_phase_main_phase_prioritization": [
        _PartSpec(file="preamble.md", role="preamble"),
        _PartSpec(
            file="two_phase_main_phase_prioritization.md",
            role="per_call",
            location="user",
            condition="rendered into user message via build_user_message(call_type=...)",
        ),
        _PartSpec(file="grounding.md", role="grounding"),
    ],
    "claim_investigation_p1": [
        _PartSpec(file="preamble.md", role="preamble"),
        _PartSpec(
            file="claim_investigation_p1.md",
            role="per_call",
            location="user",
            condition="rendered into user message via build_user_message(call_type=...)",
        ),
        _PartSpec(file="grounding.md", role="grounding"),
    ],
    "claim_investigation_p2": [
        _PartSpec(file="preamble.md", role="preamble"),
        _PartSpec(
            file="claim_investigation_p2.md",
            role="per_call",
            location="user",
            condition="rendered into user message via build_user_message(call_type=...)",
        ),
        _PartSpec(file="grounding.md", role="grounding"),
    ],
    # Scoring (called from common.py via score_items_sequentially) skips
    # citations.
    "score_subquestions": [
        _PartSpec(file="preamble.md", role="preamble"),
        _PartSpec(file="score_subquestions.md", role="per_call"),
        _PartSpec(file="grounding.md", role="grounding"),
    ],
    "score_claim_items": [
        _PartSpec(file="preamble.md", role="preamble"),
        _PartSpec(file="score_claim_items.md", role="per_call"),
        _PartSpec(file="grounding.md", role="grounding"),
    ],
    # Prioritization (the dispatch tool itself, not the variants) skips
    # citations because no pages get created.
    CallType.PRIORITIZATION.value: [
        _PartSpec(file="preamble.md", role="preamble"),
        _PartSpec(file="prioritization.md", role="per_call", optional=True),
        _PartSpec(file="grounding.md", role="grounding"),
    ],
    CallType.GLOBAL_PRIORITIZATION.value: [
        _PartSpec(file="preamble.md", role="preamble"),
        _PartSpec(file="global_prio.md", role="per_call"),
        _PartSpec(file="grounding.md", role="grounding"),
    ],
}


def _resolve_specs(call_type_value: str) -> list[_PartSpec]:
    if call_type_value in _OVERRIDES:
        return list(_OVERRIDES[call_type_value])
    return list(_DEFAULT_PARTS)


def _materialize_part(spec: _PartSpec, call_type_value: str) -> PromptPart | None:
    file_name = spec.file.replace("<call_type>", call_type_value) if spec.file else ""
    path = PROMPTS_DIR / file_name
    if not path.exists():
        if spec.optional:
            return None
        return PromptPart(
            name=file_name,
            role=spec.role,
            location=spec.location,
            condition=spec.condition,
            optional=spec.optional,
            char_count=0,
            sections=[],
            exists=False,
        )
    text = path.read_text(encoding="utf-8")
    sections = parse_prompt_sections(text)
    return PromptPart(
        name=file_name,
        role=spec.role,
        location=spec.location,
        condition=spec.condition,
        optional=spec.optional,
        char_count=len(text),
        sections=sections,
        exists=True,
    )


def build_prompt_composition(call_type_value: str) -> PromptComposition:
    """Build the system-prompt composition for a single call type."""
    specs = _resolve_specs(call_type_value)
    parts: list[PromptPart] = []
    total = 0
    for spec in specs:
        part = _materialize_part(spec, call_type_value)
        if part is None:
            continue
        parts.append(part)
        total += part.char_count
    return PromptComposition(
        call_type=call_type_value,
        parts=parts,
        total_chars=total,
    )


PSEUDO_CALL_TYPES: tuple[str, ...] = (
    "two_phase_initial_prioritization",
    "two_phase_main_phase_prioritization",
    "claim_investigation_p1",
    "claim_investigation_p2",
    "score_subquestions",
    "score_claim_items",
)


def all_prompt_keys() -> list[str]:
    """Real CallType values + pseudo-call-types declared in overrides."""
    out = [ct.value for ct in CallType]
    out.extend(PSEUDO_CALL_TYPES)
    return out


def references_for_prompt_file(file_name: str) -> list[str]:
    """Reverse-lookup: which call_type compositions cite this prompt file?"""
    refs: list[str] = []
    for key in all_prompt_keys():
        comp = build_prompt_composition(key)
        for part in comp.parts:
            if part.name == file_name:
                refs.append(key)
                break
    return sorted(set(refs))
