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
- **Per-stage prompt overrides**: each role's prompt may be replaced
  by passing a path to a markdown file (``drafter_prompt_path`` /
  ``critic_prompt_path`` / ``editor_prompt_path``). When unset, the
  built-in ``_DEFAULT_*_PROMPT`` constants are used. The fingerprint
  hashes the actual loaded text so two variants pointed at the same
  content via different paths fingerprint identically. This is the
  iterate skill's primary lever for A/B-ing prompt-text variants
  without forking the workflow file.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from rumil.budget import _consume_budget
from rumil.calls.common import mark_call_completed
from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, text_call
from rumil.model_config import ModelConfig
from rumil.models import CallStatus, CallType
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import (
    CritiqueItem,
    CritiqueRoundEvent,
    CritiqueStartedEvent,
    DraftEvent,
    DraftStartedEvent,
    EditEvent,
    EditStartedEvent,
    RoundStartedEvent,
)
from rumil.tracing.tracer import CallTrace, reset_trace, set_trace

_DEFAULT_DRAFTER_PROMPT = (
    "You are continuing an essay. The user message will give you the "
    "opening of an essay (the prefix) plus a target length. Your job is "
    "to write a substantive continuation that picks up the opening's "
    "argumentative thread.\n\n"
    "Match the opening's voice and register. Advance the argument — "
    "don't restate the opening, don't hedge performatively, don't drift "
    "generic.\n\n"
    "**Length is a hard ceiling, not a floor.** The user message gives "
    "a target character count. Treat that target as a *maximum*, not a "
    "minimum. Going materially over is a failure mode — most drafts "
    "that overshoot are padded with restatement, hedging, or "
    "under-developed elaboration. A tight draft at 80% of target beats "
    "a sprawling draft at 130%.\n\n"
    "Before the continuation, output a one-line plan in this form:\n"
    "  Plan: ~N chars, M moves: <comma-separated moves>\n"
    "where N is your self-set budget (at-or-under target) and M is the "
    "number of distinct argumentative moves you intend to make. Then "
    "write the continuation, staying at-or-under the planned N.\n\n"
    "Wrap the final continuation in <continuation>...</continuation> "
    "tags. Scratch space before the tagged block (including the Plan "
    "line) is fine; only the content inside the tags is kept."
)


_DEFAULT_CRITIC_PROMPT = (
    "You are reviewing a draft essay continuation. The user message "
    "will give you the essay opening (the prefix), the current "
    "draft continuation, and a length status (current vs target "
    "characters). Identify problems: weak arguments, factual errors, "
    "style mismatches, missed opportunities, places where the draft "
    "drifts from the opening's thread or tone. Be specific — name "
    "passages, quote phrases, point at concrete moves the writer "
    "could make.\n\n"
    "**Length awareness.** When the draft is at or above target, "
    "prefer cut suggestions over expansion suggestions — quote "
    "specific paragraphs or passages to drop, identify ideas that "
    "could be stated once instead of restated, flag tangents the "
    "piece doesn't need. Critics that only suggest additions push "
    "the editor into runaway expansion. When the draft is "
    "meaningfully below target, expansion suggestions are fine.\n\n"
    "You're not writing the next draft — an editor will read your "
    "critique alongside the others and decide what to act on. Don't "
    "hedge; don't pad with praise; don't restate what the draft "
    "already does. If a section works, it's fine to skip it.\n\n"
    "Free-form prose is expected. No need for structured edits."
)


_DEFAULT_EDITOR_PROMPT = (
    "You are revising a draft essay continuation. The user message "
    "will give you the essay opening (the prefix), the current draft, "
    "and a set of critiques from independent reviewers. Produce a "
    "revised continuation that incorporates the most important "
    "improvements while preserving what worked.\n\n"
    "**Push back on critics when they're wrong.** Critics sometimes "
    "demand changes that would hurt the piece — they may attack a move "
    "that's actually correct, push toward generic prose, or pull in "
    "incompatible directions. You are the final author. If a critic's "
    "suggestion would weaken the draft, ignore it. If two critics "
    "disagree, pick the one whose reading is closer to the opening's "
    "actual argument. Don't whiplash to satisfy every note. State "
    "briefly which critic notes you're acting on and which you're "
    "declining (and why).\n\n"
    "**Length discipline.** The user message gives both the current "
    "draft length and the target length. If the current draft is at or "
    "above target, your job is to TIGHTEN. Cutting is the primary "
    "edit. The revised continuation must be at-or-under the target. "
    "If current is close to target, edit at roughly neutral length. "
    "Only expand when the current draft is meaningfully below target "
    "and a critic identified a missing argument worth adding.\n\n"
    "**Required output format.** Before the <continuation> block, "
    "output two structured blocks in order:\n"
    "  1. <preserved>...</preserved> — a one-line note naming any "
    "passages a critic flagged as the draft's strongest move that you "
    "are keeping. Do not cut critic-flagged-strong material for "
    "length; cut elsewhere instead.\n"
    "  2. <cuts>...</cuts> — at least 3 specific cuts, one per line, "
    "in the form:\n"
    '       - Cut: "<verbatim phrase or short passage from current '
    'draft>" — Reason: <which critic note this acts on, or '
    '"redundant with X", or "over-elaborated">.\n'
    "     If you genuinely have nothing to cut (current is well below "
    "target and no critic flagged padding), say so explicitly with "
    "<cuts>none — current draft is below target and no padding "
    "flagged</cuts>.\n\n"
    "Match the opening's voice and register. Don't restate the "
    "opening.\n\n"
    "Wrap the revised continuation in <continuation>...</continuation> "
    "tags after the <preserved> and <cuts> blocks; only the content "
    "inside the <continuation> tags is kept."
)


_CONTINUATION_RE = re.compile(r"<continuation>(.*?)</continuation>", re.DOTALL | re.IGNORECASE)
_OPEN_CONTINUATION_RE = re.compile(r"<continuation>(.*)\Z", re.DOTALL | re.IGNORECASE)


def _is_truncated_continuation(text: str) -> bool:
    """True when ``text`` opens a ``<continuation>`` block but never closes it.

    The editor stage hits this when ``max_tokens`` cuts the response off
    mid-revision: the model emits the structured ``<preserved>`` /
    ``<cuts>`` scaffolding and starts the ``<continuation>`` body, then
    the API stops mid-paragraph before the closing tag. The recorded
    continuation gets accepted as-is and judges read a partial essay
    that ends mid-sentence — observed as "strongly preferred for human"
    on character × harsher_critic in the round 1 iterate session, where
    fresh re-fires of the same exchange produced complete continuations.

    Closed-block-then-open-tag is treated as not truncated — the
    closed block already carries a usable revision; the trailing open
    tag is scratch.
    """
    if _CONTINUATION_RE.search(text):
        return False
    return bool(_OPEN_CONTINUATION_RE.search(text))


def _extract_continuation(text: str) -> str:
    """Pull the final ``<continuation>...</continuation>`` block.

    Three cases:

    1. Closed block present → return its contents (stripped).
    2. Open ``<continuation>`` tag with no closer (max_tokens truncation
       with the closing tag chopped off) → return everything after the
       opener. Better than discarding hours of generation; the partial
       continuation may be salvageable.
    3. No tags at all (extremely unstructured response) → return the
       whole text stripped.

    Mirrors :func:`versus.tasks.complete_essay._extract_continuation_text`
    so the workflow's draft format matches what the task expects to
    read off ``question.content``.
    """
    matches = _CONTINUATION_RE.findall(text)
    if matches:
        return matches[-1].strip()
    open_match = _OPEN_CONTINUATION_RE.search(text)
    if open_match:
        return open_match.group(1).strip()
    return text.strip()


_PREFIX_RE = re.compile(r"## Essay opening\n\n(.+?)\n\n## Target length", re.DOTALL)


def _extract_prefix_from_question_body(content: str) -> str:
    """Pull the essay opening out of the Question body.

    :class:`versus.tasks.complete_essay.CompleteEssayTask.create_question`
    writes the prefix into the Question's content under a ``## Essay
    opening`` header followed by a ``## Target length`` block. We scrape
    it back here so the workflow can hand the bare prefix to its
    drafter / critic / editor without depending on a separate Source
    page.
    """
    m = _PREFIX_RE.search(content)
    if m is None:
        raise ValueError(
            "DraftAndEditWorkflow: no '## Essay opening' / '## Target length' "
            "block in question content; was the question created by "
            "CompleteEssayTask?"
        )
    return m.group(1).strip()


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


def _load_prompt(path: str | Path | None, default: str) -> str:
    """Resolve a prompt: load from path if given, else fall back to default.

    Path is read as UTF-8 text. Empty / whitespace-only files are an
    error — the iterate skill should not silently fingerprint a workflow
    against an unwritten prompt file.
    """
    if path is None:
        return default
    text = Path(path).read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"prompt file is empty or whitespace-only: {path}")
    return text


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
        n_critics: int = 1,
        max_rounds: int | None = None,
        drafter_model: str | None = None,
        critic_model: str | None = None,
        editor_model: str | None = None,
        drafter_prompt_path: str | Path | None = None,
        critic_prompt_path: str | Path | None = None,
        editor_prompt_path: str | Path | None = None,
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
        # Resolve prompt content at construction so fingerprint() and
        # the stage methods see the same bytes; record paths for telemetry.
        self.drafter_prompt_path = drafter_prompt_path
        self.critic_prompt_path = critic_prompt_path
        self.editor_prompt_path = editor_prompt_path
        self.drafter_prompt = _load_prompt(drafter_prompt_path, _DEFAULT_DRAFTER_PROMPT)
        self.critic_prompt = _load_prompt(critic_prompt_path, _DEFAULT_CRITIC_PROMPT)
        self.editor_prompt = _load_prompt(editor_prompt_path, _DEFAULT_EDITOR_PROMPT)
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
            "drafter_prompt_hash": _sha8(self.drafter_prompt),
            "critic_prompt_hash": _sha8(self.critic_prompt),
            "editor_prompt_hash": _sha8(self.editor_prompt),
        }

    async def setup(self, db: DB, question_id: str) -> None:
        await db.init_budget(self.budget)

    async def run(
        self,
        db: DB,
        question_id: str,
        broadcaster: Broadcaster | None,
        *,
        model_config: ModelConfig | None = None,
    ) -> None:
        question = await db.get_page(question_id)
        if question is None:
            raise RuntimeError(f"DraftAndEditWorkflow: question {question_id} missing")
        prefix = _extract_prefix_from_question_body(question.content)
        target_length = _extract_target_length_chars(question.content)

        # Persist both the raw constructor overrides (None for any knob
        # left at default — the reproducibility record) and the effective
        # values that the run actually used (resolved model ids, prompt
        # hashes, effective round cap). The trace UI renders this dict
        # verbatim, so adding the resolved values turns "null/null/null"
        # rows into something a reader can interpret without cross-
        # referencing the workflow source.
        call_params: dict[str, object] = {
            "workflow": self.name,
            "budget": self.budget,
            "n_critics": self.n_critics,
            "max_rounds": self.max_rounds,
            "effective_max_rounds": (
                self.max_rounds
                if self.max_rounds is not None
                else f"budget-bounded ({self.budget})"
            ),
            "drafter_model": self.drafter_model,
            "critic_model": self.critic_model,
            "editor_model": self.editor_model,
            "effective_drafter_model": self._resolve_model(self.drafter_model),
            "effective_critic_model": self._resolve_model(self.critic_model),
            "effective_editor_model": self._resolve_model(self.editor_model),
            "drafter_prompt_path": (
                str(self.drafter_prompt_path) if self.drafter_prompt_path else None
            ),
            "critic_prompt_path": (
                str(self.critic_prompt_path) if self.critic_prompt_path else None
            ),
            "editor_prompt_path": (
                str(self.editor_prompt_path) if self.editor_prompt_path else None
            ),
            "drafter_prompt_hash": _sha8(self.drafter_prompt),
            "critic_prompt_hash": _sha8(self.critic_prompt),
            "editor_prompt_hash": _sha8(self.editor_prompt),
        }
        call = await db.create_call(
            call_type=CallType.VERSUS_COMPLETE,
            scope_page_id=question_id,
            call_params=call_params,
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
                model_config=model_config,
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
        model_config: ModelConfig | None,
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

            await trace.record(RoundStartedEvent(round=round_idx))

            if round_idx == 0:
                current_draft = await self._draft(
                    db=db,
                    trace=trace,
                    call_id=call_id,
                    round_idx=round_idx,
                    prefix=prefix,
                    target_length=target_length,
                    model_config=model_config,
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
                    model_config=model_config,
                )

            # Skip the critique step on the final round: there's no
            # subsequent edit to consume the critiques, so paying for
            # them is dead loss. ~12% of d&e cost on a typical
            # budget=4 run was the trailing critic_round whose output
            # was never read by an editor.
            will_break_next = (
                self.max_rounds is not None and round_idx + 1 >= self.max_rounds
            ) or await db.budget_remaining() <= 0
            if not will_break_next:
                critiques = await self._critique_round(
                    db=db,
                    trace=trace,
                    call_id=call_id,
                    round_idx=round_idx,
                    prefix=prefix,
                    draft=current_draft,
                    target_length=target_length,
                    model_config=model_config,
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
        model_config: ModelConfig | None,
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
        await trace.record(DraftStartedEvent(round=round_idx, model=model))
        text = await text_call(
            self.drafter_prompt,
            user_message,
            metadata=LLMExchangeMetadata(
                call_id=call_id,
                phase="draft",
                round_num=round_idx,
            ),
            db=db,
            model=model,
            cache=True,
            model_config=model_config,
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
        target_length: int | None,
        model_config: ModelConfig | None,
    ) -> Sequence[str]:
        model = self._resolve_model(self.critic_model)
        current_chars = len(draft)
        if target_length:
            length_status = (
                f"Current draft: {current_chars} characters. "
                f"Target: {target_length} characters. "
                f"Delta: {current_chars - target_length:+d}."
            )
        else:
            length_status = f"Current draft: {current_chars} characters. (No explicit target.)"

        async def _one_critic(critic_idx: int) -> str:
            user_message = (
                "<essay-prefix>\n"
                f"{prefix}\n"
                "</essay-prefix>\n\n"
                "<draft-continuation>\n"
                f"{draft}\n"
                "</draft-continuation>\n\n"
                f"## Length\n\n{length_status}\n\n"
                "Critique this draft. Be specific and concrete."
            )
            await trace.record(
                CritiqueStartedEvent(round=round_idx, critic_index=critic_idx, model=model)
            )
            return await text_call(
                self.critic_prompt,
                user_message,
                metadata=LLMExchangeMetadata(
                    call_id=call_id,
                    phase=f"critic_r{round_idx}_n{critic_idx}",
                    round_num=round_idx,
                ),
                db=db,
                model=model,
                cache=False,
                model_config=model_config,
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
        model_config: ModelConfig | None,
    ) -> str:
        model = self._resolve_model(self.editor_model)
        critiques_block = "\n\n---\n\n".join(
            f"## Critic {i + 1}\n\n{c}" for i, c in enumerate(critiques)
        )
        current_chars = len(current_draft)
        if target_length:
            length_status = (
                f"Current draft: {current_chars} characters. "
                f"Target: {target_length} characters. "
                f"Delta: {current_chars - target_length:+d}."
            )
        else:
            length_status = f"Current draft: {current_chars} characters. (No explicit target.)"
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
            f"## Length\n\n{length_status}\n\n"
            "Produce a revised continuation. Apply the length discipline "
            "from the system prompt: tighten when current is already "
            "at-or-above target, edit at neutral length when close, only "
            "expand when meaningfully below target."
        )
        await trace.record(
            EditStartedEvent(
                round=round_idx,
                model=model,
                current_chars=current_chars,
                n_critiques=len(critiques),
            )
        )
        # Editor needs a higher output cap than the per-model default to
        # avoid mid-revision truncation. text_call disallows mixing
        # model_config with discrete max_tokens, so when a config is
        # provided clone it with the bumped cap; otherwise use the
        # discrete kwarg path.
        editor_kwargs: dict = {"cache": True}
        if model_config is not None:
            editor_kwargs["model_config"] = dataclasses.replace(model_config, max_tokens=32_000)
        else:
            editor_kwargs["max_tokens"] = 32_000
        text = await text_call(
            self.editor_prompt,
            user_message,
            metadata=LLMExchangeMetadata(
                call_id=call_id,
                phase=f"edit_r{round_idx}",
                round_num=round_idx,
            ),
            db=db,
            model=model,
            **editor_kwargs,
        )
        # If the editor's response was cut off before the closing
        # </continuation> tag, ask it to finish from where it stopped.
        # The editor's verbose <preserved> + <cuts> scaffolding is a
        # token sink; on long essays it can consume enough of the
        # max_tokens budget that the continuation body trails off
        # mid-sentence. Without this loop the partial body gets
        # accepted as-is and a judge reads a half-essay that ends in
        # the middle of a clause.
        text = await self._continue_editor_until_complete(
            db=db,
            trace=trace,
            call_id=call_id,
            round_idx=round_idx,
            initial_user_message=user_message,
            initial_response=text,
            model=model,
            editor_kwargs=editor_kwargs,
        )
        revised = _extract_continuation(text)
        # Truncated edit (closing tag still missing after continuation
        # loop, or model emitted no tags at all) → fallback. Refuse to
        # overwrite the prior draft with empty / malformed input.
        if not revised:
            revised = current_draft
        await trace.record(
            EditEvent(
                round=round_idx,
                revised_text=revised,
                revised_chars=len(revised),
                model=model,
            )
        )
        return revised

    async def _continue_editor_until_complete(
        self,
        *,
        db: DB,
        trace: CallTrace,
        call_id: str,
        round_idx: int,
        initial_user_message: str,
        initial_response: str,
        model: str,
        editor_kwargs: dict,
        max_attempts: int = 2,
    ) -> str:
        """Re-fire the editor turn-by-turn until ``<continuation>`` closes.

        When the editor's response opens ``<continuation>`` but never
        emits the closing tag, the body was cut off mid-revision by
        ``max_tokens``. The fix is a multi-turn extension: pass the
        original user message + the partial assistant reply + a brief
        "continue from where you stopped" nudge, append the new
        response, and check again. Bounded by ``max_attempts`` so a
        pathologically verbose model can't loop indefinitely.

        Returns the concatenated assistant text. Caller still passes
        the result through ``_extract_continuation`` — that function
        already tolerates an open trailing tag.
        """
        full = initial_response
        for attempt in range(max_attempts):
            if not _is_truncated_continuation(full):
                return full
            messages: list[dict] = [
                {"role": "user", "content": initial_user_message},
                {"role": "assistant", "content": full},
                {
                    "role": "user",
                    "content": (
                        "Your previous response was cut off mid-continuation — "
                        "the closing </continuation> tag is missing. Continue "
                        "from exactly where you stopped (mid-sentence is fine; "
                        "do not restate or summarize the part you already wrote). "
                        "Finish the remaining sections and end with the closing "
                        "</continuation> tag."
                    ),
                },
            ]
            more = await text_call(
                self.editor_prompt,
                messages=messages,
                metadata=LLMExchangeMetadata(
                    call_id=call_id,
                    phase=f"edit_r{round_idx}_continue{attempt + 1}",
                    round_num=round_idx,
                ),
                db=db,
                model=model,
                **editor_kwargs,
            )
            full = full + more
        return full

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
