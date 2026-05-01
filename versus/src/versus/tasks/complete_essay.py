"""Essay-completion task for versus.

Mirrors :class:`versus.tasks.judge_pair.JudgePairTask` but for the
completion path: per (essay × prefix), an orchestrator (or other
workflow) is run against a Question whose body is the essay opening
plus a "continue this essay" framing; afterwards the closer reads
whatever the workflow surfaced and emits a continuation. For
artifact-producing workflows (``produces_artifact=True``, e.g. the
DraftAndEdit workflow landing in #427) the runner skips the closer
call entirely and reads ``question.content`` verbatim — see
:func:`rumil.versus_runner.run_versus`.

Public surface:

- :class:`EssayPrefixContext` — input shape for one completion.
- :class:`CompletionArtifact` — extracted continuation + raw text.
- :class:`CompleteEssayTask` — the protocol implementation.

Hash helpers (``compute_question_surface_hash``,
``compute_completion_closer_hash``) live module-level so they hash
code/template invariants that don't depend on inputs; they're folded
into :meth:`CompleteEssayTask.fingerprint`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields

from rumil.context import format_page, render_view
from rumil.database import DB
from rumil.models import (
    CallType,
    Page,
    PageDetail,
    PageLayer,
    PageType,
    Workspace,
)
from versus.tasks.judge_pair import compute_tool_prompt_hash


@dataclass
class EssayPrefixContext:
    """Inputs for one essay-completion task.

    ``target_length_chars`` mirrors the length hint that
    :func:`versus.prepare.render_prompt` puts in the single-shot
    completion prompt — we reuse it here so orch completions aim for the
    same length as the existing single-shot rows.
    """

    essay_id: str
    prefix_hash: str
    prefix_text: str
    target_length_chars: int


@dataclass
class CompletionArtifact:
    """Structured output of one completion run.

    ``text`` is the cleaned continuation that lands in
    ``versus_texts.text``; ``raw_response`` is the full closer (or
    artifact) output retained for audit / debugging in the row's
    ``params`` blob.
    """

    text: str
    raw_response: str


def _build_headline(prefix: EssayPrefixContext) -> str:
    """Source-free headline.

    ``prefix.essay_id`` has the form ``<source>__<slug>``, so using it
    here would leak the source into headline embedding / search / tool
    output. ``prefix_hash[:8]`` uniquely identifies the (essay,
    prefix_config) pair without disclosing where the essay came from —
    same convention as the judge task.
    """
    return f"Versus completion: continue essay [{prefix.prefix_hash[:8]}]"


def _format_prefix_content(prefix: EssayPrefixContext) -> str:
    """Question body shown to the agent.

    Intentionally framed as an essay-continuation prompt — the workflow
    sees this Question and treats it as the scope of the run. Length
    target lives in the body so workflows reading the Question pick
    it up; the closer prompt also restates it for emphasis.
    """
    return (
        "This question was created by the versus essay-completion harness. "
        "An essay opening is provided below; the goal of this run is to "
        "produce a high-quality continuation that engages with the opening's "
        "topic. Workspace material may be consulted if it bears on the "
        "subject.\n\n"
        f"## Target length\n\nApproximately {prefix.target_length_chars} characters.\n\n"
        f"## Essay opening\n\n{prefix.prefix_text}\n\n"
        "## Goal\n\n"
        "Continue this essay. Aim for substantive, specific prose that picks "
        "up the opening's argumentative thread without restating it. Don't "
        "hedge performatively or drift generic."
    )


def _versus_extra(prefix: EssayPrefixContext) -> dict:
    # IMPORTANT: every key in page.extra is rendered verbatim by
    # rumil.context.format_page() (as "key: value" lines inline with
    # the page body). So anything disclosing source identity leaks
    # to the agent. Keep only neutral tags.
    #
    # `essay_id` is excluded for the same reason it's excluded on the
    # judge task — the `<source>__<slug>` namespacing bakes the source
    # into a neutral-looking id. Operator-side correlation goes through
    # ``runs.config.essay_id`` (non-agent-visible) and the row's
    # ``essay_id`` keyed via ``versus_texts``.
    return {
        "source": "versus",
        "task": "complete_essay",
        "prefix_hash": prefix.prefix_hash,
    }


def _question_surface_sentinel() -> dict[str, str | int]:
    """Sentinel inputs for hashing the Question surface.

    Built from :class:`EssayPrefixContext`'s real fields so the hash
    coverage doesn't drift from the dataclass schema — adding a field
    auto-updates the surface hash. Integer fields (e.g.
    ``target_length_chars``) get 0; string fields get a name-derived
    sentinel marker. With ``from __future__ import annotations`` in
    effect, ``f.type`` is always a string, so we match on the
    annotation text rather than the resolved type.
    """
    out: dict[str, str | int] = {}
    for f in fields(EssayPrefixContext):
        if f.type == "int":
            out[f.name] = 0
        else:
            out[f.name] = f"_SENTINEL_{f.name.upper()}_"
    return out


def compute_question_surface_hash() -> str:
    """Short deterministic hash of the Versus Question page surface.

    Folded into the task fingerprint so structural edits to the
    agent-visible page surface auto-fork the dedup key. Mirrors
    :func:`versus.tasks.judge_pair.compute_pair_surface_hash` shape.
    """
    sentinel = EssayPrefixContext(**_question_surface_sentinel())  # type: ignore[arg-type]
    blob = json.dumps(
        {
            "headline": _build_headline(sentinel),
            "content": _format_prefix_content(sentinel),
            "extra_keys": sorted(_versus_extra(sentinel).keys()),
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


_TOOL_SERVER_NAME = "versus-complete-tools"

_CLOSER_SYSTEM_PROMPT = (
    "You are a careful essay continuation writer. A research run has just "
    "finished investigating an essay opening; the rendered question (essay "
    "opening + the considerations / claims / view items the orchestrator "
    "produced) follows in the user message. Your job is to read it and emit "
    "a finished continuation that engages with the opening's argument. "
    "Do not restate the opening. Do not hedge performatively. Do not drift "
    "generic. Workspace tools are available if further material bears on "
    "the subject, but keep usage light — this is the closing step, not a "
    "fresh investigation."
)
_CLOSER_USER_PROMPT_TEMPLATE = (
    "{rendered}\n\n"
    "Write a continuation of approximately {target_length_chars} characters. "
    "Wrap the final continuation in <continuation>...</continuation> tags; "
    "only the content inside those tags is recorded. You may use scratch "
    "space before the tagged block to plan, outline, or note dead ends — "
    "anything outside the tags is discarded."
)
_CLOSER_SDK_MAX_TURNS = 5
_CLOSER_DISALLOWED_TOOLS = ("Write", "Edit", "Glob")
_CLOSER_RENDER_DETAIL = "CONTENT"
_CLOSER_RENDER_LINKED_DETAIL = "CONTENT"
_CLOSER_RENDER_MIN_IMPORTANCE = 2


def compute_completion_closer_hash() -> str:
    """Short deterministic hash of the closer's invariant config.

    Covers the system prompt, the user prompt template, the SDK agent's
    max_turns budget, the disallowed-tools set, and the rendering knobs.
    Folded into the task fingerprint so an edit here auto-forks the
    dedup key.
    """
    blob = json.dumps(
        {
            "system_prompt": _CLOSER_SYSTEM_PROMPT,
            "user_prompt_template": _CLOSER_USER_PROMPT_TEMPLATE,
            "sdk_agent_max_turns": _CLOSER_SDK_MAX_TURNS,
            "disallowed_tools": list(_CLOSER_DISALLOWED_TOOLS),
            "render_detail": _CLOSER_RENDER_DETAIL,
            "render_linked_detail": _CLOSER_RENDER_LINKED_DETAIL,
            "render_min_importance": _CLOSER_RENDER_MIN_IMPORTANCE,
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


_CONTINUATION_RE = re.compile(r"<continuation>(.*?)</continuation>", re.DOTALL | re.IGNORECASE)


def _extract_continuation_text(text: str) -> str:
    """Pull the final ``<continuation>...</continuation>`` block.

    Mirrors :func:`versus.complete.extract_continuation` so orch
    completions and single-shot completions land with the same
    text-cleanup contract. Falls back to the whole text (stripped) when
    the model omits the tags entirely so we never persist an empty
    string from a well-formed-but-untagged continuation.
    """
    matches = _CONTINUATION_RE.findall(text)
    if matches:
        return matches[-1].strip()
    return text.strip()


class CompleteEssayTask:
    """Essay-completion task — produces a finished continuation."""

    name = "complete_essay"
    sdk_max_turns: int = _CLOSER_SDK_MAX_TURNS
    disallowed_tools: tuple[str, ...] = _CLOSER_DISALLOWED_TOOLS
    tool_server_name: str = _TOOL_SERVER_NAME

    def fingerprint(self, inputs: EssayPrefixContext) -> Mapping[str, str | int | bool | None]:
        # Note: no ``pair_surface_hash`` — there's no pair. The Question
        # surface is hashed via ``question_surface_hash`` instead.
        return {
            "kind": self.name,
            "tool_prompt_hash": compute_tool_prompt_hash(),
            "question_surface_hash": compute_question_surface_hash(),
            "closer_hash": compute_completion_closer_hash(),
        }

    async def create_question(self, db: DB, inputs: EssayPrefixContext) -> str:
        """Create the scope Question for one completion run."""
        page = Page(
            page_type=PageType.QUESTION,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content=_format_prefix_content(inputs),
            headline=_build_headline(inputs),
            project_id=db.project_id,
            provenance_model="versus-bridge",
            provenance_call_type=CallType.VERSUS_JUDGE.value,
            run_id=db.run_id,
            extra=_versus_extra(inputs),
        )
        await db.save_page(page)
        return page.id

    async def render_for_closer(self, db: DB, question_id: str) -> str:
        """Render the Question + research subgraph for the closer.

        Uses the same render shape as :class:`JudgePairTask` — Question
        body at CONTENT detail (so linked considerations / judgements
        surface their bodies, not just headlines) plus the View page
        and its items at importance >= 2. The closer reads this and
        synthesizes the continuation.

        For ``produces_artifact=True`` workflows the runner skips the
        closer entirely and reads ``question.content`` directly; this
        path is only taken on research workflows like TwoPhase.
        """
        question = await db.get_page(question_id)
        if question is None:
            raise RuntimeError(f"question {question_id} missing after orch run")
        body = await format_page(
            question,
            PageDetail.CONTENT,
            linked_detail=PageDetail.CONTENT,
            db=db,
        )
        view = await db.get_view_for_question(question_id)
        if view is None:
            return body
        items = await db.get_view_items(view.id, min_importance=2)
        view_rendered = await render_view(view, items, min_importance=2)
        return f"{body}\n\n{view_rendered}"

    def closer_prompts(self, rendered: str, inputs: EssayPrefixContext) -> tuple[str, str]:
        system = _CLOSER_SYSTEM_PROMPT
        user = _CLOSER_USER_PROMPT_TEMPLATE.format(
            rendered=rendered,
            target_length_chars=inputs.target_length_chars,
        )
        return system, user

    def extract_artifact(self, closer_text: str) -> CompletionArtifact:
        """Strip <continuation> tags and surrounding scratch.

        ``raw_response`` retains the full closer output so the
        ``versus_texts`` row's ``params`` blob preserves any planning
        scratch the model emitted before the tagged continuation.
        """
        cleaned = _extract_continuation_text(closer_text)
        return CompletionArtifact(text=cleaned, raw_response=closer_text)
