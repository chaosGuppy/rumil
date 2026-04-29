"""ContextBuilder wrapper that runs an impact-percentile filter on top of a
base builder's output.

Pipeline:

1. Run the inner builder to produce a "standard context".
2. (Optional) If the standard context exceeds ``pare_threshold_tokens``, ask
   Opus to score each page in it for absolute importance and keep the top-N
   that fit ``pare_target_tokens``.
3. BFS d <= ``max_distance`` from the question across research-graph edges to
   gather candidate evidence pages (claims, judgements, summaries) not already
   in the (post-pared) context.
4. Score each candidate concurrently with Sonnet (configurable) for marginal
   impact on top of the standard context. Output is a 1-100 percentile.
5. Sort by percentile descending; greedily fill until total chars (context +
   accepted) approach ``token_budget`` or the next page's percentile drops
   below ``floor_percentile``. Floor is hard (never include below 25 by
   default).
6. Render accepted candidates the same way the base builder renders its full
   tier (no separate heading) and append them to the context. The LLM cannot
   tell which pages came from the inner builder vs. the impact filter.

Smoke-test bypass: when ``settings.is_smoke_test`` is True, the wrapper
returns the inner builder's result unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from pydantic import BaseModel, Field

from rumil.calls.stages import CallInfra, ContextBuilder, ContextResult
from rumil.context import format_page
from rumil.llm import structured_call
from rumil.models import Page, PageDetail
from rumil.settings import get_settings
from rumil.tracing.trace_events import ImpactFilterEvent
from rumil.workspace_exploration.explore import bfs_evidence_pages_within_distance

log = logging.getLogger(__name__)


class ImpactVerdict(BaseModel):
    new_information: str = Field(
        description=(
            "1-3 sentences listing what — if anything — this candidate page "
            "would add that is NOT already in the context. Be specific. If "
            "the page just restates or provides additional support for "
            "claims already in the context, say so."
        )
    )
    impact_reasoning: str = Field(
        description=(
            "Step-by-step reasoning about marginal impact. Address: (a) does "
            "the page surface a finding, frame, mechanism, quantitative "
            "anchor, or argument that is genuinely absent from the context; "
            "(b) does it RESOLVE an uncertainty visible in the context; "
            "(c) is the page logically on the question (cf. conditional / "
            "counterfactual structure) — orthogonal pages get low percentiles "
            "regardless of internal quality; (d) would an analyst writing "
            "the final answer notably revise their take if they read this "
            "page after reading the context?"
        )
    )
    impact_percentile: int = Field(
        ge=1,
        le=100,
        description=(
            "Where this candidate falls in the distribution of MARGINAL "
            "impacts of pages-on-top-of-this-context. ANCHORS:\n"
            "- 90-100: would substantively change how the analyst writes "
            "the final answer — surfaces a load-bearing fact, frame, or "
            "uncertainty resolution that meaningfully shifts the headline "
            "conclusion or its key probabilities.\n"
            "- 70-89: surfaces specific empirical content, a mechanism, a "
            "quantitative anchor, or a frame that is genuinely absent from "
            "the context. The analyst would CITE it and would MENTION it as "
            "load-bearing for a sub-conclusion, even if the headline "
            "conclusion doesn't change.\n"
            "- 50-69: has some new content but is largely supporting / "
            "elaborative on points already in the context. Useful as a "
            "citation, not as a structural input.\n"
            "- 25-49: substantially redundant with context.\n"
            "- 1-24: redundant, off-topic given the question's logical "
            "structure, on a tangent (e.g. estimates P(X) when the question "
            "conditions on X), or actively misleading.\n"
            "\n"
            "CALIBRATION CHECK before locking in:\n"
            "1. If `new_information` lists at least one specific empirical "
            "claim/mechanism/frame NOT in the context, score should be >= 50; "
            "if multiple distinct items or one item load-bearing for a named "
            "sub-question, score should be >= 70.\n"
            "2. If reasoning concludes 'on a tangent' or 'estimates P(X) when "
            "the question conditions on X', score should be <= 24 regardless "
            "of internal quality.\n"
            "3. Across many candidates expect a roughly uniform distribution "
            "with median around 50."
        ),
    )


class ParingVerdict(BaseModel):
    importance_reasoning: str = Field(
        description=(
            "1-3 sentences explaining how load-bearing this page is for "
            "answering the top-level question. Address whether it surfaces "
            "a key finding, frame, or uncertainty resolution; whether it is "
            "logically on-question; whether dropping it would leave a hole "
            "in the analysis."
        )
    )
    importance_score: int = Field(
        ge=1,
        le=100,
        description=(
            "1-100 score for absolute importance to the top-level question. "
            "ANCHORS:\n"
            "- 90-100: load-bearing synthesis or finding the analyst cannot "
            "write the answer without.\n"
            "- 70-89: concrete distinctive content with clear cascade to "
            "the top-level question; should appear unless redundant.\n"
            "- 50-69: solid supporting detail; cited rather than structural.\n"
            "- 25-49: marginal — adds little to the answer.\n"
            "- 1-24: off-topic, tangential to the literal question, or noise."
        ),
    )


IMPACT_SYSTEM = (
    "You are a senior research analyst evaluating whether a single candidate "
    "page would add MARGINAL value to a final answer that has already been "
    "informed by the standard context shown.\n\n"
    "Your task is NOT 'is this page relevant to the question'. The question "
    "of relevance is largely already settled: the standard context already "
    "captures the main shape of the answer. Your task is the harder one: "
    "given that an analyst is going to write the final answer using AT LEAST "
    "the standard context, how much would also reading this candidate page "
    "change what they would write?\n\n"
    "FRAMING — read the top-level question literally:\n"
    "- If the question is CONDITIONAL on X, pages whose primary contribution "
    "is to estimate P(X) are not load-bearing — they should be low-percentile "
    "regardless of internal quality.\n"
    "- If the question is COUNTERFACTUAL, comparisons across the counter"
    "factual axis are load-bearing; one-sided base rates are not.\n"
    "- Pages that drift onto tangents that don't propagate to the top-level "
    "question are low-percentile.\n\n"
    "MARGINAL-VALUE PRINCIPLES — what makes a page HIGH-impact ON TOP OF "
    "this specific context:\n"
    "- It surfaces a *new finding* (specific empirical claim, mechanism, "
    "quantitative estimate, historical analogue) that is not already present "
    "in the context.\n"
    "- It introduces a *new frame* — a way of decomposing the problem, an "
    "axis the context doesn't recognise, a reframing that materially changes "
    "the answer's structure.\n"
    "- It RESOLVES an uncertainty visible in the context — the context "
    "hedges, this page tightens; the context offers a range, this page "
    "narrows it; the context flags a missing input, this page provides it.\n\n"
    "What makes a page LOW-impact even when on-topic:\n"
    "- It restates or merely *bolsters* a claim already in the context.\n"
    "- It elaborates a sub-mechanism whose top-line conclusion is already "
    "in the context.\n"
    "- It is on a tangent.\n"
    "- It contains rough Fermi estimates of quantities the context already "
    "estimates.\n\n"
    "CALIBRATION — aim for a roughly uniform distribution of percentile "
    "scores. Median candidate should land around 50. Persistent bunching "
    "at 60-80 is a calibration failure: be willing to assign 10s, 20s, "
    "and 90s when warranted."
)


PARING_SYSTEM = (
    "You are a senior research analyst rating how IMPORTANT a single page "
    "is for answering the top-level research question, on its own merits.\n\n"
    "This is not a marginal-value judgement against another context — there "
    "is no 'context' here, just the question and the page. Think: if you "
    "had to drop this page from the analyst's source material, would the "
    "final answer be materially worse?\n\n"
    "FRAMING — read the top-level question literally:\n"
    "- If the question is CONDITIONAL on X, pages whose primary contribution "
    "is to estimate P(X) are not load-bearing.\n"
    "- If the question is COUNTERFACTUAL, comparisons across the counter"
    "factual axis are load-bearing; one-sided base rates are not.\n"
    "- Pages on tangents that don't propagate to the top-level question are "
    "low-importance regardless of internal quality.\n\n"
    "Reward pages that carry distinctive, load-bearing information: a "
    "specific empirical finding, a key mechanism, a historical analogue with "
    "real explanatory power, a sharp epistemic update, a quantitative "
    "estimate, or a sound argument that materially shapes the answer.\n\n"
    "Penalise pages that are vague, generic, redundant with what any "
    "reasonable analyst would already know, or that restate the sub-question "
    "without adding evidence."
)


def _render_candidate_user_msg(top_question: str, context_text: str, sample: Page) -> str:
    """User message for the impact-percentile call. The system prompt + the "
    "first part of this message (top question + context) are stable across "
    "candidates and will be cached."""
    parts: list[str] = [
        "# Top-level research question\n\n",
        top_question.strip(),
        "\n\n# Standard context already available to the analyst\n\n",
        "(Built by the workspace's standard context builder. Treat this as "
        "the baseline — the analyst will write their answer using at minimum "
        "this context. Your job is to judge marginal value of the candidate "
        "ON TOP OF this.)\n\n",
        context_text,
        "\n\n# Candidate page under review\n\n",
        f"Type: {sample.page_type.value}\n",
        f"Headline: {sample.headline}\n",
    ]
    if sample.credence is not None:
        parts.append(f"Author-assigned credence: {sample.credence}/9\n")
    if sample.robustness is not None:
        parts.append(f"Author-assigned robustness: {sample.robustness}/5\n")
    parts.append("\n## Full content\n\n")
    parts.append(sample.content)
    parts.append("\n\n# Your task\n\n")
    parts.append(
        "Decide what (if anything) this candidate page would add to the "
        "final answer that is not already conveyed by the standard context. "
        "Then assign an impact percentile per the rubric in the schema."
    )
    return "".join(parts)


def _render_paring_user_msg(top_question: str, page: Page) -> str:
    parts: list[str] = [
        "# Top-level research question\n\n",
        top_question.strip(),
        "\n\n# Page under review\n\n",
        f"Type: {page.page_type.value}\n",
        f"Headline: {page.headline}\n",
    ]
    if page.credence is not None:
        parts.append(f"Author-assigned credence: {page.credence}/9\n")
    if page.robustness is not None:
        parts.append(f"Author-assigned robustness: {page.robustness}/5\n")
    parts.append("\n## Full content\n\n")
    parts.append(page.content)
    parts.append("\n\n# Your task\n\n")
    parts.append(
        "Rate this page's importance for answering the top-level question on "
        "the 1-100 scale defined in the schema."
    )
    return "".join(parts)


class ImpactFilteredContext(ContextBuilder):
    """Wraps an inner ContextBuilder, then runs an impact-percentile filter
    pipeline that adds high-marginal-value pages from the d <= max_distance
    subgraph until a token budget is filled.

    Configuration falls back to settings when params are None.
    """

    def __init__(
        self,
        inner_builder: ContextBuilder,
        *,
        scoring_model: str | None = None,
        pare_model: str | None = None,
        token_budget: int | None = None,
        floor_percentile: int | None = None,
        pare_threshold_tokens: int | None = None,
        pare_target_tokens: int | None = None,
        max_distance: int | None = None,
        concurrency: int | None = None,
    ) -> None:
        self._inner = inner_builder
        self._scoring_model = scoring_model
        self._pare_model = pare_model
        self._token_budget = token_budget
        self._floor_percentile = floor_percentile
        self._pare_threshold_tokens = pare_threshold_tokens
        self._pare_target_tokens = pare_target_tokens
        self._max_distance = max_distance
        self._concurrency = concurrency

    async def build_context(self, infra: CallInfra) -> ContextResult:
        settings = get_settings()
        scoring_model = self._scoring_model or settings.impact_filter_scoring_model
        pare_model = self._pare_model or settings.impact_filter_pare_model
        token_budget = self._token_budget or settings.impact_filter_token_budget
        floor_percentile = (
            self._floor_percentile
            if self._floor_percentile is not None
            else settings.impact_filter_floor_percentile
        )
        pare_threshold_tokens = (
            self._pare_threshold_tokens or settings.impact_filter_pare_threshold_tokens
        )
        pare_target_tokens = self._pare_target_tokens or settings.impact_filter_pare_target_tokens
        max_distance = self._max_distance or settings.impact_filter_max_distance
        concurrency = self._concurrency or settings.impact_filter_concurrency

        inner_result = await self._inner.build_context(infra)

        if settings.is_smoke_test:
            log.info("ImpactFilteredContext: smoke-test bypass")
            return inner_result

        question = await infra.db.get_page(infra.question_id)
        top_question_text = question.content if question else infra.question_id

        char_budget = token_budget * 4
        pare_threshold_chars = pare_threshold_tokens * 4
        pare_target_chars = pare_target_tokens * 4

        pared_result = inner_result
        paring_triggered = False
        paring_kept_pages: int | None = None
        paring_kept_chars: int | None = None
        if len(inner_result.context_text) > pare_threshold_chars:
            paring_triggered = True
            pared_result = await self._pare_inner_context(
                infra,
                inner_result,
                top_question_text=top_question_text,
                pare_model=pare_model,
                pare_target_chars=pare_target_chars,
                concurrency=concurrency,
            )
            paring_kept_pages = (
                len(pared_result.full_page_ids)
                + len(pared_result.abstract_page_ids)
                + len(pared_result.summary_page_ids)
                + len(pared_result.distillation_page_ids)
            )
            paring_kept_chars = len(pared_result.context_text)

        excluded_ids: set[str] = set()
        excluded_ids.update(pared_result.working_page_ids)
        excluded_ids.update(pared_result.preloaded_ids)
        # Belt-and-braces: build_context tracks page IDs across tiers, but if a
        # builder mutates `working_page_ids` we still want all the tier IDs out.
        excluded_ids.update(pared_result.full_page_ids)
        excluded_ids.update(pared_result.abstract_page_ids)
        excluded_ids.update(pared_result.summary_page_ids)
        excluded_ids.update(pared_result.distillation_page_ids)

        candidates = await bfs_evidence_pages_within_distance(
            infra.question_id,
            infra.db,
            max_distance=max_distance,
        )
        candidates = [p for p in candidates if p.id not in excluded_ids]
        log.info(
            "ImpactFilteredContext: %d candidate evidence pages at d<=%d after exclusions",
            len(candidates),
            max_distance,
        )

        scored = await self._score_candidates(
            candidates,
            top_question_text=top_question_text,
            inner_context_text=pared_result.context_text,
            scoring_model=scoring_model,
            concurrency=concurrency,
        )

        accepted, _total_chars, threshold = self._select_within_budget(
            scored,
            base_chars=len(pared_result.context_text),
            char_budget=char_budget,
            floor_percentile=floor_percentile,
        )

        appended_text = await self._render_accepted(infra, accepted)
        merged_text = pared_result.context_text
        if appended_text:
            merged_text = pared_result.context_text + "\n\n" + appended_text

        merged_full_ids: list[str] = list(pared_result.full_page_ids) + [p.id for p in accepted]
        merged_working_ids: list[str] = list(pared_result.working_page_ids) + [
            p.id for p in accepted if p.id not in set(pared_result.working_page_ids)
        ]
        merged_budget = dict(pared_result.budget_usage)
        merged_budget["impact_filter"] = sum(len(p.content or "") for p in accepted)

        await infra.trace.record(
            ImpactFilterEvent(
                inner_context_chars=len(inner_result.context_text),
                paring_triggered=paring_triggered,
                paring_kept_pages=paring_kept_pages,
                paring_kept_chars=paring_kept_chars,
                candidates_scored=len(scored),
                candidates_accepted=len(accepted),
                accepted_chars=merged_budget["impact_filter"],
                final_threshold_percentile=threshold,
                scoring_model=scoring_model,
                pare_model=pare_model if paring_triggered else None,
            )
        )

        return ContextResult(
            context_text=merged_text,
            working_page_ids=merged_working_ids,
            preloaded_ids=pared_result.preloaded_ids,
            full_page_ids=merged_full_ids,
            abstract_page_ids=pared_result.abstract_page_ids,
            summary_page_ids=pared_result.summary_page_ids,
            distillation_page_ids=pared_result.distillation_page_ids,
            budget_usage=merged_budget,
            source_page_id=pared_result.source_page_id,
        )

    async def _score_candidates(
        self,
        candidates: Sequence[Page],
        *,
        top_question_text: str,
        inner_context_text: str,
        scoring_model: str,
        concurrency: int,
    ) -> list[tuple[Page, ImpactVerdict]]:
        if not candidates:
            return []
        sem = asyncio.Semaphore(concurrency)

        async def _score_one(page: Page) -> tuple[Page, ImpactVerdict] | None:
            async with sem:
                msg = _render_candidate_user_msg(top_question_text, inner_context_text, page)
                try:
                    result = await structured_call(
                        system_prompt=IMPACT_SYSTEM,
                        user_message=msg,
                        response_model=ImpactVerdict,
                        model=scoring_model,
                        cache=True,
                        parse_manually=False,
                    )
                except Exception as e:
                    log.warning("impact-filter scoring failed for %s: %s", page.id[:8], e)
                    return None
                if result.parsed is None:
                    log.warning("impact-filter scoring returned None for %s", page.id[:8])
                    return None
                return page, result.parsed

        results = await asyncio.gather(*(_score_one(p) for p in candidates))
        return [r for r in results if r is not None]

    @staticmethod
    def _select_within_budget(
        scored: Sequence[tuple[Page, ImpactVerdict]],
        *,
        base_chars: int,
        char_budget: int,
        floor_percentile: int,
    ) -> tuple[list[Page], int, int]:
        ordered = sorted(scored, key=lambda item: -item[1].impact_percentile)
        accepted: list[Page] = []
        total = base_chars
        threshold = 100
        for page, verdict in ordered:
            if verdict.impact_percentile < floor_percentile:
                break
            page_chars = len(page.content or "")
            if total + page_chars > char_budget:
                break
            accepted.append(page)
            total += page_chars
            threshold = verdict.impact_percentile
        return accepted, total, threshold

    async def _render_accepted(self, infra: CallInfra, accepted: Sequence[Page]) -> str:
        if not accepted:
            return ""
        rendered: list[str] = []
        for page in accepted:
            text = await format_page(
                page,
                PageDetail.CONTENT,
                db=infra.db,
                linked_detail=None,
                track=True,
                track_tags={"source": "impact_filter_full"},
            )
            rendered.append(text)
        return "\n\n".join(rendered)

    async def _pare_inner_context(
        self,
        infra: CallInfra,
        inner_result: ContextResult,
        *,
        top_question_text: str,
        pare_model: str,
        pare_target_chars: int,
        concurrency: int,
    ) -> ContextResult:
        """Score every page in inner_result by absolute importance and keep
        the top-N pages whose combined re-rendered length fits
        pare_target_chars.

        Each page is re-rendered at its original tier fidelity. Pages whose
        importance call fails are kept (we'd rather over-include than
        accidentally drop a critical page on a transient API blip)."""
        full_ids = list(inner_result.full_page_ids)
        abstract_ids = list(inner_result.abstract_page_ids)
        summary_ids = list(inner_result.summary_page_ids)
        distillation_ids = list(inner_result.distillation_page_ids)

        tier_by_id: dict[str, PageDetail] = {}
        for pid in distillation_ids:
            tier_by_id[pid] = PageDetail.CONTENT
        for pid in full_ids:
            tier_by_id.setdefault(pid, PageDetail.CONTENT)
        for pid in abstract_ids:
            tier_by_id.setdefault(pid, PageDetail.ABSTRACT)
        for pid in summary_ids:
            tier_by_id.setdefault(pid, PageDetail.HEADLINE)

        all_ids = list(tier_by_id.keys())
        if not all_ids:
            log.info("ImpactFilteredContext: paring requested but no tier IDs to score")
            return inner_result

        pages_by_id = await infra.db.get_pages_by_ids(all_ids)
        sem = asyncio.Semaphore(concurrency)

        async def _score_one(pid: str) -> tuple[str, int]:
            page = pages_by_id.get(pid)
            if page is None:
                return pid, 0
            async with sem:
                try:
                    result = await structured_call(
                        system_prompt=PARING_SYSTEM,
                        user_message=_render_paring_user_msg(top_question_text, page),
                        response_model=ParingVerdict,
                        model=pare_model,
                        cache=True,
                        parse_manually=False,
                    )
                except Exception as e:
                    log.warning("paring importance call failed for %s: %s", pid[:8], e)
                    return pid, 100  # keep on failure
                if result.parsed is None:
                    return pid, 100
                return pid, result.parsed.importance_score

        scores = dict(await asyncio.gather(*(_score_one(pid) for pid in all_ids)))

        rendered_lengths: dict[str, int] = {}
        rendered_text: dict[str, str] = {}
        for pid in all_ids:
            page = pages_by_id.get(pid)
            if page is None:
                continue
            tier = tier_by_id[pid]
            text = await format_page(
                page,
                tier,
                db=infra.db,
                linked_detail=None,
                track=True,
                track_tags={"source": "impact_filter_pared"},
            )
            rendered_text[pid] = text
            rendered_lengths[pid] = len(text)

        ordered = sorted(rendered_text.keys(), key=lambda pid: -scores.get(pid, 0))
        kept: list[str] = []
        total = 0
        for pid in ordered:
            length = rendered_lengths[pid]
            if total + length > pare_target_chars and kept:
                break
            kept.append(pid)
            total += length

        kept_set = set(kept)
        new_full = [pid for pid in full_ids if pid in kept_set]
        new_abstract = [pid for pid in abstract_ids if pid in kept_set]
        new_summary = [pid for pid in summary_ids if pid in kept_set]
        new_distillation = [pid for pid in distillation_ids if pid in kept_set]

        new_text = "\n\n".join(rendered_text[pid] for pid in kept)
        new_working = [pid for pid in inner_result.working_page_ids if pid in kept_set]

        log.info(
            "ImpactFilteredContext: paring kept %d/%d pages (%d chars, target %d)",
            len(kept),
            len(all_ids),
            total,
            pare_target_chars,
        )

        return ContextResult(
            context_text=new_text,
            working_page_ids=new_working,
            preloaded_ids=inner_result.preloaded_ids,
            full_page_ids=new_full,
            abstract_page_ids=new_abstract,
            summary_page_ids=new_summary,
            distillation_page_ids=new_distillation,
            budget_usage={**inner_result.budget_usage, "paring_kept_chars": total},
            source_page_id=inner_result.source_page_id,
        )
