"""ReflectiveJudgeWorkflow — pairwise judging via read → reflect → verdict.

Versus-specific judge workflow. Three sequential ``text_call`` stages
(initial read, reflective critique, final verdict) write the verdict
into ``question.content`` for the runner's ``produces_artifact=True``
path to extract.

Why this exists:

- The blind one-shot judge is cheap but has zero structure to iterate
  on — there's nowhere to apply lessons from a trace+fork loop.
- The orch judge fires :class:`TwoPhaseOrchestrator`, which is shared
  with non-versus rumil callers; we don't want to fiddle its prompts
  or stages.
- This workflow is fully independent of two_phase. No prompts, helpers,
  or stage logic are shared. It's the iteration target on the judging
  side: change any of the three stage prompts, swap a model per role,
  or add stages freely without touching anything outside this file.

Design notes:

- **Stage shape**: read → reflect → verdict.
  - Read is the initial assessment — what each text is doing, where
    they diverge, distinctive moves, surface judgments.
  - Reflect interrogates the read — what's potentially biased, shallow,
    or wrong about that initial pass; steelmans the disfavored side;
    surfaces uncertainties.
  - Verdict synthesizes both prior stages into a final 7-point
    preference label with reasoning.
- **Per-role models**: any of ``reader_model`` / ``reflector_model`` /
  ``verdict_model`` may be set independently. ``None`` means inherit
  the bridge-set ``rumil_model_override``.
- **Per-stage prompt overrides**: each stage's prompt may be replaced
  by passing a path to a markdown file (``read_prompt_path`` /
  ``reflect_prompt_path`` / ``verdict_prompt_path``). When unset, the
  built-in ``_DEFAULT_*_PROMPT`` constants are used. The fingerprint
  hashes the actual loaded text, not the path, so two variants pointed
  at the same content via different paths fingerprint identically.
  This is the iterate skill's primary lever for A/B-ing prompt-text
  variants without forking the workflow file.
- **Dimension body** (the rubric for the dimension being judged —
  e.g. ``versus-general_quality.md``) is shared with the orch and
  blind paths intentionally. The rubric is what the dimension *is*;
  it's not workflow-specific. The workflow's iteration surface is the
  three stage prompts, not the rubric.
- **No budget consumption**: budget is fixed at 3 LLM calls per run.
  ``setup`` seeds a budget row of total=3 for telemetry parity with
  other workflows; ``run`` doesn't consume from it.
- **Tracing**: each stage's ``text_call`` records an ``LLMExchange``
  with ``phase`` set to ``read`` / ``reflect`` / ``verdict``. No
  dedicated trace events — the exchanges themselves are the trace.
  Add events later if the iterate loop wants more structure.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path

from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, text_call
from rumil.models import CallStatus, CallType
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import (
    ReadStartedEvent,
    ReflectStartedEvent,
    VerdictStartedEvent,
)
from rumil.tracing.tracer import CallTrace, reset_trace, set_trace

_DEFAULT_READ_PROMPT = (
    "You are the READ stage of a structured pairwise judgment of two "
    "essay continuations. The user message will give you the dimension "
    "rubric, the essay opening (the prefix), and two continuations "
    "labeled A and B.\n\n"
    "Your job in this stage is to produce a careful initial reading of "
    "the pair against the rubric. Do not yet commit to a verdict — that "
    "comes later. Specifically:\n"
    "- Name what each continuation is doing argumentatively, structurally, "
    "and stylistically. Quote distinctive moves where useful.\n"
    "- Identify the places where the two diverge — different framings, "
    "different argument shapes, different rhetorical strategies.\n"
    "- Score each text against the rubric's specific criteria, with "
    "concrete evidence. Where one text clearly outperforms the other on "
    "a criterion, say so plainly; where it's close or mixed, say that.\n"
    "- Note any first-pass observations about strengths, weaknesses, or "
    "puzzles you'd want to revisit.\n\n"
    "Be specific and grounded. Don't hedge performatively. Don't restate "
    "the rubric. Free-form prose is fine; structure with headers if it "
    "helps. This output will be read by a downstream Reflect stage and a "
    "Verdict stage — write so they can use what you produced.\n\n"
    "Do NOT emit a 7-point preference label in this stage. Save the "
    "verdict for the verdict stage."
)


_DEFAULT_REFLECT_PROMPT = (
    "You are the REFLECT stage of a structured pairwise judgment. The "
    "user message will give you the dimension rubric, the essay opening, "
    "the two continuations (A and B), and the prior READ stage's output.\n\n"
    "Your job is to interrogate the prior reading. Specifically:\n"
    "- Where might the read be biased toward A or B for the wrong "
    "reason? Length, surface fluency, citation density, and stylistic "
    "polish are common confounders that don't track the rubric — call "
    "them out if the read is leaning on them.\n"
    "- Where might the read be shallow? Are there moves in either "
    "continuation it didn't engage with? Quote them.\n"
    "- Steelman the disfavored side. If the read leaned toward A, give "
    "the strongest honest reading of B against this rubric, and vice "
    "versa. The goal isn't to flip the verdict — it's to make sure the "
    "verdict that does emerge has actually beaten the strongest "
    "available counterargument.\n"
    "- Surface remaining uncertainties: facts you can't verify, "
    "ambiguities in the rubric's application, dimensions where the "
    "evidence is genuinely close.\n\n"
    "Be willing to mostly endorse the read if it's solid — don't "
    "manufacture disagreement. But push back specifically and "
    "concretely where the read missed something. Free-form prose. Don't "
    "emit a 7-point preference label here either."
)


_DEFAULT_VERDICT_PROMPT = (
    "You are the VERDICT stage of a structured pairwise judgment. The "
    "user message will give you the dimension rubric, the essay opening, "
    "the two continuations (A and B), and the outputs of the prior READ "
    "and REFLECT stages.\n\n"
    "Your job: synthesize a final verdict on which continuation better "
    "satisfies the dimension's rubric. The prior stages did the "
    "evidence-gathering and self-criticism — your job is to weigh and "
    "decide.\n\n"
    "Write a verdict of 2-5 paragraphs that:\n"
    "1. States the headline conclusion in the first sentence.\n"
    "2. Names the load-bearing reasons (the moves or properties that "
    "actually drove the call), citing specific passages.\n"
    "3. Acknowledges what the losing side does well — the verdict "
    "should be reachable by someone who reads both texts independently.\n"
    "4. Calibrates strength honestly. If the gap is large and durable, "
    "say so. If the gap is real but narrow, or rubric-dependent, say "
    "that and explain.\n\n"
    "**Calibration discipline against the prior reflection.** If the "
    "REFLECT stage explicitly argued the gap is narrower than the READ "
    "stage suggested, you must either (a) step the label down by one "
    "notch on the 7-point scale (e.g. 'somewhat preferred' → 'slightly "
    "preferred'; 'slightly preferred' → 'Approximately indifferent'), "
    "or (b) explain in one sentence why the reflect's narrowing "
    "argument fails. Do not anchor on the read's strength claim while "
    "paying lip service to reflect in prose.\n\n"
    "End your response with exactly one of these labels on its own "
    "line, nothing else after it:\n"
    "  A strongly preferred\n"
    "  A somewhat preferred\n"
    "  A slightly preferred\n"
    "  Approximately indifferent between A and B\n"
    "  B slightly preferred\n"
    "  B somewhat preferred\n"
    "  B strongly preferred\n\n"
    "The downstream harness extracts the label by string match — copy "
    "it verbatim, no quotes, no surrounding text on the same line."
)


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


class ReflectiveJudgeWorkflow:
    """Versus pairwise judge: read → reflect → verdict.

    See module docstring for design rationale. Implements the
    :class:`rumil.versus_workflow.Workflow` protocol.
    """

    name: str = "reflective_judge"
    produces_artifact: bool = True
    code_paths: Sequence[str] = ("src/rumil/orchestrators/reflective_judge.py",)
    relevant_settings: Sequence[str] = ()

    def __init__(
        self,
        *,
        dimension_body: str,
        reader_model: str | None = None,
        reflector_model: str | None = None,
        verdict_model: str | None = None,
        read_prompt_path: str | Path | None = None,
        reflect_prompt_path: str | Path | None = None,
        verdict_prompt_path: str | Path | None = None,
    ) -> None:
        if not dimension_body or not dimension_body.strip():
            raise ValueError("dimension_body is required and must be non-empty")
        self.dimension_body = dimension_body
        self.reader_model = reader_model
        self.reflector_model = reflector_model
        self.verdict_model = verdict_model
        # Resolve prompt content at construction so fingerprint() and
        # _run_stages see the same bytes; record paths for telemetry only.
        self.read_prompt_path = read_prompt_path
        self.reflect_prompt_path = reflect_prompt_path
        self.verdict_prompt_path = verdict_prompt_path
        self.read_prompt = _load_prompt(read_prompt_path, _DEFAULT_READ_PROMPT)
        self.reflect_prompt = _load_prompt(reflect_prompt_path, _DEFAULT_REFLECT_PROMPT)
        self.verdict_prompt = _load_prompt(verdict_prompt_path, _DEFAULT_VERDICT_PROMPT)
        self.last_status: str = "complete"

    def fingerprint(self) -> Mapping[str, str | int | bool | None]:
        return {
            "kind": self.name,
            "reader_model": self.reader_model,
            "reflector_model": self.reflector_model,
            "verdict_model": self.verdict_model,
            "dimension_body_hash": _sha8(self.dimension_body),
            "read_prompt_hash": _sha8(self.read_prompt),
            "reflect_prompt_hash": _sha8(self.reflect_prompt),
            "verdict_prompt_hash": _sha8(self.verdict_prompt),
        }

    async def setup(self, db: DB, question_id: str) -> None:
        # Fixed 3 LLM calls per run; budget row is purely for telemetry parity.
        await db.init_budget(3)

    async def run(
        self,
        db: DB,
        question_id: str,
        broadcaster: Broadcaster | None,
    ) -> None:
        question = await db.get_page(question_id)
        if question is None:
            raise RuntimeError(f"ReflectiveJudgeWorkflow: question {question_id} missing")
        pair_content = question.content

        call = await db.create_call(
            call_type=CallType.VERSUS_JUDGE,
            scope_page_id=question_id,
            call_params={
                "workflow": self.name,
                "reader_model": self.reader_model,
                "reflector_model": self.reflector_model,
                "verdict_model": self.verdict_model,
                "read_prompt_path": str(self.read_prompt_path) if self.read_prompt_path else None,
                "reflect_prompt_path": (
                    str(self.reflect_prompt_path) if self.reflect_prompt_path else None
                ),
                "verdict_prompt_path": (
                    str(self.verdict_prompt_path) if self.verdict_prompt_path else None
                ),
            },
        )
        await db.update_call_status(call.id, CallStatus.RUNNING)
        trace = CallTrace(call.id, db, broadcaster=broadcaster)
        trace_token = set_trace(trace)
        try:
            verdict_text = await self._run_stages(
                db=db,
                trace=trace,
                call_id=call.id,
                pair_content=pair_content,
            )
            await db.update_page_content(question_id, verdict_text)
        finally:
            reset_trace(trace_token)

        from rumil.calls.common import mark_call_completed

        await mark_call_completed(call, db, summary="reflective_judge: complete")

    async def _run_stages(
        self,
        *,
        db: DB,
        trace: CallTrace,
        call_id: str,
        pair_content: str,
    ) -> str:
        rubric_block = f"<dimension-rubric>\n{self.dimension_body}\n</dimension-rubric>"
        pair_block = f"<pair>\n{pair_content}\n</pair>"

        read_user_message = f"{rubric_block}\n\n{pair_block}\n\nProduce the initial read."
        read_model = self._resolve_model(self.reader_model)
        await trace.record(ReadStartedEvent(model=read_model))
        read_text = await text_call(
            self.read_prompt,
            read_user_message,
            metadata=LLMExchangeMetadata(call_id=call_id, phase="read", round_num=0),
            db=db,
            model=read_model,
            cache=True,
        )

        reflect_user_message = (
            f"{rubric_block}\n\n"
            f"{pair_block}\n\n"
            "<prior-read>\n"
            f"{read_text}\n"
            "</prior-read>\n\n"
            "Interrogate the prior read."
        )
        reflect_model = self._resolve_model(self.reflector_model)
        await trace.record(
            ReflectStartedEvent(model=reflect_model, prior_read_chars=len(read_text))
        )
        reflect_text = await text_call(
            self.reflect_prompt,
            reflect_user_message,
            metadata=LLMExchangeMetadata(call_id=call_id, phase="reflect", round_num=1),
            db=db,
            model=reflect_model,
            cache=True,
        )

        verdict_user_message = (
            f"{rubric_block}\n\n"
            f"{pair_block}\n\n"
            "<prior-read>\n"
            f"{read_text}\n"
            "</prior-read>\n\n"
            "<prior-reflection>\n"
            f"{reflect_text}\n"
            "</prior-reflection>\n\n"
            "Produce the final verdict and the 7-point preference label."
        )
        verdict_model = self._resolve_model(self.verdict_model)
        await trace.record(
            VerdictStartedEvent(
                model=verdict_model,
                prior_read_chars=len(read_text),
                prior_reflect_chars=len(reflect_text),
            )
        )
        verdict_text = await text_call(
            self.verdict_prompt,
            verdict_user_message,
            metadata=LLMExchangeMetadata(call_id=call_id, phase="verdict", round_num=2),
            db=db,
            model=verdict_model,
            cache=True,
        )
        return verdict_text

    def _resolve_model(self, override: str | None) -> str:
        """Resolve a per-role model override.

        Same precedence as :class:`DraftAndEditWorkflow._resolve_model` —
        explicit constructor kwarg wins, then ``rumil_model_override``
        from settings (the standard ``run_versus`` path sets this), then
        fail-loud. Falling back to ``settings.model`` would let
        non-bridge instantiations silently use the wrong model.
        """
        if override is not None:
            return override
        rmo = get_settings().rumil_model_override
        if rmo:
            return rmo
        raise RuntimeError(
            "ReflectiveJudgeWorkflow requires a model — pass via "
            "constructor (reader_model / reflector_model / verdict_model) "
            "or via override_settings(rumil_model_override=...) (the "
            "run_versus path sets this automatically from its `model` arg)."
        )
