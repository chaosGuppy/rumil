"""DraftAndEditWorkflow — SDK-driven essay completion via draft → critique → edit.

Distinct from the BudgetedOrchWorkflow base because there's no rumil
orchestrator wrapped here: the workflow drives a small fixed pipeline
of plain ``text_call`` LLMs (drafter → N parallel critics → editor) per
round and stores the final draft on ``question.content`` for the
versus runner's ``produces_artifact=True`` path to read.

Design notes (full sketch in ``planning/draft-and-edit-workflow-sketch.md``):

- **Spawn pattern**: ``asyncio.gather`` over ``text_call`` per critic.
  Critics need no tools, no autonomy, and benefit from per-role model
  overrides — the SDK's nested ``Agent`` tool would be heavier than
  warranted here.
- **Where intermediates live**: trace events (``DraftEvent``,
  ``CritiqueRoundEvent``, ``EditEvent``) on the workflow's call —
  not workspace pages. Critic prose on the page graph would pollute
  embedding search and risks leaking essay prefix material into
  unrelated workspace surfaces under blind-judging.
- **Where the final draft lives**: ``question.content`` via
  ``db.update_page_content`` (mutation event aware). The runner reads
  it verbatim and feeds it to ``CompleteEssayTask.extract_artifact``.
- **Budget**: 1 unit per outer round. One round = drafter (or editor)
  + N critics. Budget consumed at the top of each round; if exhausted
  before any draft was produced ``last_status="incomplete"``.
- **Per-role models**: drafter / critic / editor models can differ via
  constructor kwargs; ``None`` means "inherit the rumil_model_override
  from settings", which is what ``run_versus`` sets from the caller's
  ``--model``.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping, Sequence

from rumil.budget import _consume_budget
from rumil.calls.common import mark_call_completed
from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, text_call
from rumil.models import CallStatus, CallType, LinkType, PageType
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import (
    CritiqueItem,
    CritiqueRoundEvent,
    DraftEvent,
    EditEvent,
)
from rumil.tracing.tracer import CallTrace, reset_trace, set_trace

_DRAFTER_PROMPT = (
    "You are continuing an essay. The user message will give you the "
    "opening of an essay (the prefix) plus a target length. Your job is "
    "to write a substantive continuation that picks up the opening's "
    "argumentative thread.\n\n"
    "Match the opening's voice and register. Advance the argument — "
    "don't restate the opening, don't hedge performatively, don't drift "
    "generic. Aim for the target length but prefer a tighter, sharper "
    "continuation over padding.\n\n"
    "Wrap the final continuation in <continuation>...</continuation> "
    "tags. You may use scratch space before the tagged block to plan, "
    "outline, or note dead ends; only the content inside the tags is "
    "kept."
)


_CRITIC_PROMPT = (
    "You are reviewing a draft essay continuation. The user message "
    "will give you the essay opening (the prefix) and the current "
    "draft continuation. Identify problems: weak arguments, factual "
    "errors, style mismatches, missed opportunities, places where the "
    "draft drifts from the opening's thread or tone. Be specific — "
    "name passages, quote phrases, point at concrete moves the writer "
    "could make.\n\n"
    "You're not writing the next draft — an editor will read your "
    "critique alongside the others and decide what to act on. Don't "
    "hedge; don't pad with praise; don't restate what the draft "
    "already does. If a section works, it's fine to skip it.\n\n"
    "Free-form prose is expected. No need for structured edits."
)


_EDITOR_PROMPT = (
    "You are revising a draft essay continuation. The user message "
    "will give you the essay opening (the prefix), the current draft, "
    "and a set of critiques from independent reviewers. Produce a "
    "revised continuation that incorporates the most important "
    "improvements while preserving what worked.\n\n"
    "You may ignore critiques you disagree with — critics sometimes "
    "contradict each other or push in directions that hurt the piece. "
    "Use judgement; don't whiplash the draft trying to satisfy "
    "everyone.\n\n"
    "Match the opening's voice and register. Don't restate the "
    "opening. Aim for roughly the target length given in the user "
    "message.\n\n"
    "Wrap the revised continuation in <continuation>...</continuation> "
    "tags. Scratch space before the tagged block is fine; only the "
    "content inside the tags is kept."
)


_CONTINUATION_RE = re.compile(r"<continuation>(.*?)</continuation>", re.DOTALL | re.IGNORECASE)


def _extract_continuation(text: str) -> str:
    """Pull the final ``<continuation>...</continuation>`` block.

    Falls back to the whole text (stripped) when no tags are present.
    Mirrors :func:`versus.tasks.complete_essay._extract_continuation_text`
    so the workflow's draft format matches what the task expects to
    read off ``question.content``.
    """
    matches = _CONTINUATION_RE.findall(text)
    if matches:
        return matches[-1].strip()
    return text.strip()


async def _load_prefix_from_linked_source(db: DB, question_id: str) -> str:
    """Pull the essay opening from the Source page linked to the Question.

    :class:`versus.tasks.complete_essay.CompleteEssayTask.create_question`
    writes the prefix to a separate Source page and links it to the
    Question via ``LinkType.RELATED`` (source -> question). We round-trip
    the same shape here so the workflow can hand the bare prefix to its
    drafter / critic / editor.
    """
    incoming = await db.get_links_to(question_id)
    source_link_ids = [link.from_page_id for link in incoming if link.link_type == LinkType.RELATED]
    if not source_link_ids:
        raise ValueError(
            "DraftAndEditWorkflow: no RELATED link into the question; "
            "was the question created by CompleteEssayTask?"
        )
    pages = await db.get_pages_by_ids(source_link_ids)
    sources = [p for p in pages.values() if p.page_type == PageType.SOURCE]
    if not sources:
        raise ValueError(
            "DraftAndEditWorkflow: no linked Source page on the question; "
            "was the question created by CompleteEssayTask?"
        )
    if len(sources) > 1:
        raise ValueError(
            "DraftAndEditWorkflow: multiple Source pages linked to the "
            f"question (found {len(sources)}); ambiguous prefix lookup."
        )
    return sources[0].content


_TARGET_LENGTH_RE = re.compile(r"Approximately\s+(\d+)\s+characters\.")


def _extract_target_length_chars(content: str) -> int | None:
    """Pull the target-length hint out of the Question body.

    :class:`versus.tasks.complete_essay._format_prefix_framing` writes
    ``Approximately {N} characters.`` under a ``## Target length``
    header. We surface it to the drafter / editor so they can aim at
    the same length single-shot completions target.
    """
    m = _TARGET_LENGTH_RE.search(content)
    if m is None:
        return None
    return int(m.group(1))


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


class DraftAndEditWorkflow:
    """SDK-driven essay completion via draft → critique → edit loops.

    See module docstring for design rationale. Implements the
    :class:`rumil.versus_workflow.Workflow` protocol.
    """

    name: str = "draft_and_edit"
    produces_artifact: bool = True
    code_paths: Sequence[str] = ("src/rumil/orchestrators/draft_and_edit.py",)
    relevant_settings: Sequence[str] = ()

    def __init__(
        self,
        *,
        budget: int,
        n_critics: int = 2,
        max_rounds: int | None = None,
        drafter_model: str | None = None,
        critic_model: str | None = None,
        editor_model: str | None = None,
    ) -> None:
        if budget < 1:
            raise ValueError(f"budget must be >= 1, got {budget}")
        if n_critics < 1:
            raise ValueError(f"n_critics must be >= 1, got {n_critics}")
        if max_rounds is not None and max_rounds < 1:
            raise ValueError(f"max_rounds must be >= 1 or None, got {max_rounds}")
        self.budget = budget
        self.n_critics = n_critics
        self.max_rounds = max_rounds
        self.drafter_model = drafter_model
        self.critic_model = critic_model
        self.editor_model = editor_model
        self.last_status: str = "complete"

    def fingerprint(self) -> Mapping[str, str | int | bool | None]:
        return {
            "kind": self.name,
            "budget": self.budget,
            "n_critics": self.n_critics,
            "max_rounds": self.max_rounds,
            "drafter_model": self.drafter_model,
            "critic_model": self.critic_model,
            "editor_model": self.editor_model,
            "drafter_prompt_hash": _sha8(_DRAFTER_PROMPT),
            "critic_prompt_hash": _sha8(_CRITIC_PROMPT),
            "editor_prompt_hash": _sha8(_EDITOR_PROMPT),
        }

    async def setup(self, db: DB, question_id: str) -> None:
        await db.init_budget(self.budget)

    async def run(
        self,
        db: DB,
        question_id: str,
        broadcaster: Broadcaster | None,
    ) -> None:
        question = await db.get_page(question_id)
        if question is None:
            raise RuntimeError(f"DraftAndEditWorkflow: question {question_id} missing")
        prefix = await _load_prefix_from_linked_source(db, question_id)
        target_length = _extract_target_length_chars(question.content)

        call = await db.create_call(
            call_type=CallType.VERSUS_JUDGE,
            scope_page_id=question_id,
            call_params={
                "workflow": self.name,
                "n_critics": self.n_critics,
                "max_rounds": self.max_rounds,
                "drafter_model": self.drafter_model,
                "critic_model": self.critic_model,
                "editor_model": self.editor_model,
            },
        )
        await db.update_call_status(call.id, CallStatus.RUNNING)
        trace = CallTrace(call.id, db, broadcaster=broadcaster)
        trace_token = set_trace(trace)
        try:
            await self._run_loop(
                db=db,
                trace=trace,
                call_id=call.id,
                question_id=question_id,
                prefix=prefix,
                target_length=target_length,
            )
            await mark_call_completed(call, db, summary=f"draft_and_edit: {self.last_status}")
        finally:
            reset_trace(trace_token)

    async def _run_loop(
        self,
        *,
        db: DB,
        trace: CallTrace,
        call_id: str,
        question_id: str,
        prefix: str,
        target_length: int | None,
    ) -> None:
        """Iterate draft → critique → edit until budget or max_rounds bites.

        Round 0 produces the initial draft; rounds 1..N each fold one
        round of critique into the draft via the editor. Budget is
        consumed at the top of each round so we never stop mid-round.
        """
        current_draft: str = ""
        critiques: Sequence[str] = []
        round_idx = 0
        while True:
            if self.max_rounds is not None and round_idx >= self.max_rounds:
                break
            if not await _consume_budget(db):
                if round_idx == 0:
                    self.last_status = "incomplete"
                break

            if round_idx == 0:
                current_draft = await self._draft(
                    db=db,
                    trace=trace,
                    call_id=call_id,
                    round_idx=round_idx,
                    prefix=prefix,
                    target_length=target_length,
                )
            else:
                current_draft = await self._edit(
                    db=db,
                    trace=trace,
                    call_id=call_id,
                    round_idx=round_idx,
                    prefix=prefix,
                    target_length=target_length,
                    current_draft=current_draft,
                    critiques=critiques,
                )

            critiques = await self._critique_round(
                db=db,
                trace=trace,
                call_id=call_id,
                round_idx=round_idx,
                prefix=prefix,
                draft=current_draft,
            )
            round_idx += 1

        if not current_draft:
            return
        await db.update_page_content(question_id, current_draft)

    async def _draft(
        self,
        *,
        db: DB,
        trace: CallTrace,
        call_id: str,
        round_idx: int,
        prefix: str,
        target_length: int | None,
    ) -> str:
        model = self._resolve_model(self.drafter_model)
        target_clause = (
            f"Aim for approximately {target_length} characters." if target_length else ""
        )
        user_message = (
            "<essay-prefix>\n"
            f"{prefix}\n"
            "</essay-prefix>\n\n"
            "Continue this essay. Match the opening's voice and "
            "advance the argument. "
            f"{target_clause}".strip()
        )
        text = await text_call(
            _DRAFTER_PROMPT,
            user_message,
            metadata=LLMExchangeMetadata(
                call_id=call_id,
                phase="draft",
                round_num=round_idx,
            ),
            db=db,
            model=model,
            cache=True,
        )
        draft = _extract_continuation(text)
        await trace.record(
            DraftEvent(
                round=round_idx,
                draft_text=draft,
                draft_chars=len(draft),
                model=model,
            )
        )
        return draft

    async def _critique_round(
        self,
        *,
        db: DB,
        trace: CallTrace,
        call_id: str,
        round_idx: int,
        prefix: str,
        draft: str,
    ) -> Sequence[str]:
        model = self._resolve_model(self.critic_model)

        async def _one_critic(critic_idx: int) -> str:
            user_message = (
                "<essay-prefix>\n"
                f"{prefix}\n"
                "</essay-prefix>\n\n"
                "<draft-continuation>\n"
                f"{draft}\n"
                "</draft-continuation>\n\n"
                "Critique this draft. Be specific and concrete."
            )
            return await text_call(
                _CRITIC_PROMPT,
                user_message,
                metadata=LLMExchangeMetadata(
                    call_id=call_id,
                    phase=f"critic_r{round_idx}_n{critic_idx}",
                    round_num=round_idx,
                ),
                db=db,
                model=model,
                cache=False,
            )

        critiques = await asyncio.gather(*(_one_critic(i) for i in range(self.n_critics)))
        await trace.record(
            CritiqueRoundEvent(
                round=round_idx,
                critiques=[
                    CritiqueItem(critic_index=i, critique_text=c, model=model)
                    for i, c in enumerate(critiques)
                ],
            )
        )
        return critiques

    async def _edit(
        self,
        *,
        db: DB,
        trace: CallTrace,
        call_id: str,
        round_idx: int,
        prefix: str,
        target_length: int | None,
        current_draft: str,
        critiques: Sequence[str],
    ) -> str:
        model = self._resolve_model(self.editor_model)
        critiques_block = "\n\n---\n\n".join(
            f"## Critic {i + 1}\n\n{c}" for i, c in enumerate(critiques)
        )
        target_clause = (
            f"Aim for approximately {target_length} characters." if target_length else ""
        )
        user_message = (
            "<essay-prefix>\n"
            f"{prefix}\n"
            "</essay-prefix>\n\n"
            "<current-draft>\n"
            f"{current_draft}\n"
            "</current-draft>\n\n"
            "<critiques>\n"
            f"{critiques_block}\n"
            "</critiques>\n\n"
            f"Produce a revised continuation. {target_clause}".strip()
        )
        text = await text_call(
            _EDITOR_PROMPT,
            user_message,
            metadata=LLMExchangeMetadata(
                call_id=call_id,
                phase=f"edit_r{round_idx}",
                round_num=round_idx,
            ),
            db=db,
            model=model,
            cache=True,
        )
        revised = _extract_continuation(text)
        await trace.record(
            EditEvent(
                round=round_idx,
                revised_text=revised,
                revised_chars=len(revised),
                model=model,
            )
        )
        return revised

    def _resolve_model(self, override: str | None) -> str:
        """Resolve a per-role model override.

        Precedence: explicit constructor kwarg → ``rumil_model_override``
        (the standard ``run_versus`` path sets this via
        :func:`override_settings`) → fail-loud. Per ``versus/AGENT.md``:
        "Model for orch is passed explicitly through the bridge ... do
        not rely on ``settings.model``." Silently falling back to ambient
        ``settings.model`` would let non-bridge instantiations (tests,
        future scripts) use whatever happened to be in settings; better
        to fail fast.
        """
        if override is not None:
            return override
        rmo = get_settings().rumil_model_override
        if rmo:
            return rmo
        raise RuntimeError(
            "DraftAndEditWorkflow requires a model — pass via constructor "
            "(drafter_model / critic_model / editor_model) or via "
            "override_settings(rumil_model_override=...) (the run_versus "
            "path sets this automatically from its `model` arg)."
        )
