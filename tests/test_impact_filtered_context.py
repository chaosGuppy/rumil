"""Unit tests for ImpactFilteredContext.

Verify the wrapper's wiring without going through the LLM:
- smoke-test bypass returns the inner result unchanged
- selection respects budget + floor percentile
- pages already in the inner context are excluded from candidates
- accepted candidates are merged into the result's tier IDs
- paring trigger fires only when inner context exceeds threshold
"""

from __future__ import annotations

import pytest_asyncio
from pydantic import BaseModel

from rumil.calls.impact_filtered_context import (
    ImpactFilteredContext,
    ImpactVerdict,
)
from rumil.calls.stages import CallInfra, ContextBuilder, ContextResult
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.moves.base import MoveState
from rumil.settings import override_settings
from rumil.tracing.tracer import CallTrace


def _make_evidence(headline: str, content: str, page_type: PageType = PageType.CLAIM) -> Page:
    return Page(
        page_type=page_type,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=content,
        headline=headline,
        credence=6,
        robustness=3,
    )


class _FakeStructuredCallResult:
    def __init__(self, parsed: BaseModel):
        self.parsed = parsed


class _StubInnerBuilder(ContextBuilder):
    """Returns a fixed ContextResult — no DB, no LLM."""

    def __init__(self, result: ContextResult) -> None:
        self._result = result

    async def build_context(self, infra: CallInfra) -> ContextResult:
        return self._result


def _scoring_call_text(kwargs: dict) -> str:
    """Concatenate the text of every content block in a scoring call's
    `messages` kwarg, so existing tests can substring-search for
    candidate-specific markers without caring about block layout."""
    messages = kwargs.get("messages") or []
    chunks: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    chunks.append(block.get("text", ""))
    return "".join(chunks)


@pytest_asyncio.fixture
async def view_call(tmp_db, question_page):
    call = Call(
        call_type=CallType.CREATE_VIEW_MAX_EFFORT,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    return call


def _make_infra(db, call, question_page) -> CallInfra:
    return CallInfra(
        question_id=question_page.id,
        call=call,
        db=db,
        trace=CallTrace(call.id, db),
        state=MoveState(call, db),
    )


async def test_smoke_test_bypass(mocker, tmp_db, question_page, view_call):
    """When settings.is_smoke_test is True, the wrapper returns inner verbatim
    and never calls structured_call or BFS."""
    inner = ContextResult(
        context_text="STANDARD CONTEXT",
        working_page_ids=["a", "b"],
        full_page_ids=["a"],
        abstract_page_ids=["b"],
        budget_usage={"full": 100, "abstract": 50},
    )
    bfs_spy = mocker.patch(
        "rumil.calls.impact_filtered_context.bfs_evidence_pages_within_distance",
        new_callable=mocker.AsyncMock,
        return_value=[],
    )
    structured_spy = mocker.patch(
        "rumil.calls.impact_filtered_context.structured_call",
        new_callable=mocker.AsyncMock,
    )
    wrapper = ImpactFilteredContext(inner_builder=_StubInnerBuilder(inner))
    infra = _make_infra(tmp_db, view_call, question_page)
    with override_settings(rumil_smoke_test="1"):
        result = await wrapper.build_context(infra)
    assert result is inner
    bfs_spy.assert_not_called()
    structured_spy.assert_not_called()


async def test_excludes_inner_pages_from_candidates(mocker, tmp_db, question_page, view_call):
    """Pages whose IDs already appear in the inner result aren't re-scored."""
    inner_page = _make_evidence("In context", "X" * 200)
    candidate = _make_evidence("Outside context", "Y" * 200)
    inner = ContextResult(
        context_text="STANDARD CONTEXT",
        working_page_ids=[inner_page.id],
        full_page_ids=[inner_page.id],
    )
    mocker.patch(
        "rumil.calls.impact_filtered_context.bfs_evidence_pages_within_distance",
        new_callable=mocker.AsyncMock,
        return_value=[inner_page, candidate],  # BFS returns both; wrapper filters
    )

    seen_ids: list[str] = []

    async def fake_structured_call(**kwargs):
        # Capture which page got scored (look for a unique fragment in user msg)
        msg = _scoring_call_text(kwargs)
        if inner_page.headline in msg:
            seen_ids.append(inner_page.id)
        if candidate.headline in msg:
            seen_ids.append(candidate.id)
        return _FakeStructuredCallResult(
            ImpactVerdict(new_information="x", impact_reasoning="y", impact_percentile=50)
        )

    mocker.patch(
        "rumil.calls.impact_filtered_context.structured_call",
        side_effect=fake_structured_call,
    )

    wrapper = ImpactFilteredContext(inner_builder=_StubInnerBuilder(inner))
    infra = _make_infra(tmp_db, view_call, question_page)
    with override_settings(rumil_smoke_test=""):
        await wrapper.build_context(infra)

    assert candidate.id in seen_ids
    assert inner_page.id not in seen_ids


async def test_scoring_calls_share_cached_prefix(mocker, tmp_db, question_page, view_call):
    """Every scoring call must put cache_control on a prefix block that's
    byte-identical across candidates, so calls 2..N hit the prompt cache.

    Regression test for the rate-limit storm we hit when running the impact
    filter on big questions: the old code passed the question + standard
    context + per-candidate content as one user_message string with
    cache=True, which puts the cache breakpoint at the END (per-candidate,
    never reused). Each call paid full input cost on the ~50k-token shared
    prefix → TPM blowout → 429s → SDK + tenacity retries → call count well
    above the candidate count.
    """
    candidates = [_make_evidence(f"Candidate {i}", f"Body {i}" * 20) for i in range(3)]
    inner = ContextResult(
        context_text="A" * 500,
        working_page_ids=[],
    )
    mocker.patch(
        "rumil.calls.impact_filtered_context.bfs_evidence_pages_within_distance",
        new_callable=mocker.AsyncMock,
        return_value=candidates,
    )

    captured: list[dict] = []

    async def fake_structured_call(**kwargs):
        captured.append(kwargs)
        return _FakeStructuredCallResult(
            ImpactVerdict(new_information="x", impact_reasoning="y", impact_percentile=50)
        )

    mocker.patch(
        "rumil.calls.impact_filtered_context.structured_call",
        side_effect=fake_structured_call,
    )

    wrapper = ImpactFilteredContext(inner_builder=_StubInnerBuilder(inner))
    infra = _make_infra(tmp_db, view_call, question_page)
    with override_settings(rumil_smoke_test=""):
        await wrapper.build_context(infra)

    assert len(captured) == len(candidates)
    prefix_texts: list[str] = []
    for kwargs in captured:
        # Must use the messages= path, not user_message= (the old single-string
        # path can't place cache_control on a shared prefix block).
        assert kwargs.get("user_message") in (None, ""), (
            "scoring call must pass `messages=` so cache_control sits on the "
            "shared prefix; the user_message= path collapses everything into "
            "one block and defeats caching"
        )
        messages = kwargs["messages"]
        assert len(messages) == 1
        content = messages[0]["content"]
        assert isinstance(content, list) and len(content) >= 2
        prefix_block, *rest = content
        assert prefix_block["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in rest[-1], (
            "per-candidate suffix block must not carry cache_control — that's "
            "what reproduces the original miss"
        )
        prefix_texts.append(prefix_block["text"])

    assert len(set(prefix_texts)) == 1, (
        "shared prefix text differs across calls — it would not cache-hit"
    )
    assert candidates[0].headline not in prefix_texts[0], (
        "candidate-specific text leaked into the cached prefix"
    )


async def test_excludes_excluded_page_ids(mocker, tmp_db, question_page, view_call):
    """Pages listed in inner_result.excluded_page_ids are never scored.

    UpdateViewContext lists pages already cited by existing view items as
    excluded; the wrapper must respect that exclusion even though those
    pages don't appear in working_page_ids or any tier ID list.
    """
    cited_page = _make_evidence("Already cited by an existing view item", "C" * 200)
    candidate = _make_evidence("Fresh candidate", "Y" * 200)
    inner = ContextResult(
        context_text="STANDARD CONTEXT",
        working_page_ids=[],
        excluded_page_ids=[cited_page.id],
    )
    mocker.patch(
        "rumil.calls.impact_filtered_context.bfs_evidence_pages_within_distance",
        new_callable=mocker.AsyncMock,
        return_value=[cited_page, candidate],
    )

    seen_ids: list[str] = []

    async def fake_structured_call(**kwargs):
        msg = _scoring_call_text(kwargs)
        if cited_page.headline in msg:
            seen_ids.append(cited_page.id)
        if candidate.headline in msg:
            seen_ids.append(candidate.id)
        return _FakeStructuredCallResult(
            ImpactVerdict(new_information="x", impact_reasoning="y", impact_percentile=50)
        )

    mocker.patch(
        "rumil.calls.impact_filtered_context.structured_call",
        side_effect=fake_structured_call,
    )

    wrapper = ImpactFilteredContext(inner_builder=_StubInnerBuilder(inner))
    infra = _make_infra(tmp_db, view_call, question_page)
    with override_settings(rumil_smoke_test=""):
        await wrapper.build_context(infra)

    assert candidate.id in seen_ids
    assert cited_page.id not in seen_ids


async def test_excludes_pages_tracked_via_trace(mocker, tmp_db, question_page, view_call):
    """Pages rendered by the inner builder via format_page(track=True) are
    excluded even when the builder doesn't surface their IDs in
    working_page_ids / tier IDs / excluded_page_ids.

    CreateViewContext renders existing view items into the context but
    doesn't track their page IDs in any returned list. The wrapper must
    still exclude them via the trace's page-load record so sonnet doesn't
    waste a call re-scoring a page that's already in the prompt.
    """
    untracked_page = _make_evidence("Rendered but untracked", "U" * 200)
    candidate = _make_evidence("Fresh candidate", "Y" * 200)

    class _TraceRecordingInnerBuilder(ContextBuilder):
        async def build_context(self, infra: CallInfra) -> ContextResult:
            infra.trace.record_page_load(
                untracked_page.id,
                "content",
                {"source": "embedding_full"},
            )
            return ContextResult(
                context_text="STANDARD CONTEXT (incl. untracked page)",
                working_page_ids=[],
            )

    mocker.patch(
        "rumil.calls.impact_filtered_context.bfs_evidence_pages_within_distance",
        new_callable=mocker.AsyncMock,
        return_value=[untracked_page, candidate],
    )

    seen_ids: list[str] = []

    async def fake_structured_call(**kwargs):
        msg = _scoring_call_text(kwargs)
        if untracked_page.headline in msg:
            seen_ids.append(untracked_page.id)
        if candidate.headline in msg:
            seen_ids.append(candidate.id)
        return _FakeStructuredCallResult(
            ImpactVerdict(new_information="x", impact_reasoning="y", impact_percentile=50)
        )

    mocker.patch(
        "rumil.calls.impact_filtered_context.structured_call",
        side_effect=fake_structured_call,
    )

    wrapper = ImpactFilteredContext(inner_builder=_TraceRecordingInnerBuilder())
    infra = _make_infra(tmp_db, view_call, question_page)
    with override_settings(rumil_smoke_test=""):
        await wrapper.build_context(infra)

    assert candidate.id in seen_ids
    assert untracked_page.id not in seen_ids


async def test_floor_percentile_drops_low_scores(mocker, tmp_db, question_page, view_call):
    """Pages scored below floor_percentile are not included even if budget allows."""
    inner = ContextResult(context_text="ctx", working_page_ids=[])
    high = _make_evidence("High impact", "H" * 100)
    low = _make_evidence("Low impact", "L" * 100)
    mocker.patch(
        "rumil.calls.impact_filtered_context.bfs_evidence_pages_within_distance",
        new_callable=mocker.AsyncMock,
        return_value=[high, low],
    )

    async def fake_structured_call(**kwargs):
        msg = _scoring_call_text(kwargs)
        score = 80 if "High impact" in msg else 10
        return _FakeStructuredCallResult(
            ImpactVerdict(new_information="x", impact_reasoning="y", impact_percentile=score)
        )

    mocker.patch(
        "rumil.calls.impact_filtered_context.structured_call",
        side_effect=fake_structured_call,
    )

    wrapper = ImpactFilteredContext(inner_builder=_StubInnerBuilder(inner))
    infra = _make_infra(tmp_db, view_call, question_page)
    with override_settings(
        rumil_smoke_test="",
        impact_filter_floor_percentile=25,
        impact_filter_token_budget=200_000,
    ):
        result = await wrapper.build_context(infra)

    assert high.id in result.full_page_ids
    assert low.id not in result.full_page_ids


async def test_budget_caps_acceptance(mocker, tmp_db, question_page, view_call):
    """When candidates would overflow the budget, only the highest-scored
    pages that fit are accepted."""
    inner = ContextResult(context_text="x" * 100, working_page_ids=[])
    big_pages = [_make_evidence(f"Page {i}", "C" * 1000) for i in range(5)]
    mocker.patch(
        "rumil.calls.impact_filtered_context.bfs_evidence_pages_within_distance",
        new_callable=mocker.AsyncMock,
        return_value=big_pages,
    )
    # Score in declining order: the i-th page in big_pages gets percentile 90-i*10.
    page_scores = {p.id: 90 - i * 10 for i, p in enumerate(big_pages)}

    def _name_in(msg: str, pid: str, pages: list[Page]) -> bool:
        return any(p.id == pid and p.headline in msg for p in pages)

    async def fake_structured_call(**kwargs):
        msg = _scoring_call_text(kwargs)
        score = next(s for p, s in page_scores.items() if p in msg or _name_in(msg, p, big_pages))
        return _FakeStructuredCallResult(
            ImpactVerdict(new_information="x", impact_reasoning="y", impact_percentile=score)
        )

    mocker.patch(
        "rumil.calls.impact_filtered_context.structured_call",
        side_effect=fake_structured_call,
    )

    wrapper = ImpactFilteredContext(inner_builder=_StubInnerBuilder(inner))
    infra = _make_infra(tmp_db, view_call, question_page)

    # token_budget is in tokens (4 chars per token). 600 tokens = 2400 chars budget.
    # Inner: 100 chars. Each big page: 1000 chars. So 2 fit (100+1000+1000=2100),
    # the third (3100) would overflow.
    with override_settings(
        rumil_smoke_test="",
        impact_filter_floor_percentile=1,
        impact_filter_token_budget=600,
    ):
        result = await wrapper.build_context(infra)

    accepted = [pid for pid in result.full_page_ids if pid in {p.id for p in big_pages}]
    assert len(accepted) == 2
    # Highest-percentile pages should be the ones accepted.
    expected = [big_pages[0].id, big_pages[1].id]
    assert set(accepted) == set(expected)


async def test_paring_skipped_below_threshold(mocker, tmp_db, question_page, view_call):
    """If inner context is under pare_threshold_tokens, paring isn't triggered.

    We assert by spying on the trace.record call to capture the
    ImpactFilterEvent and checking paring_triggered=False.
    """
    inner = ContextResult(context_text="x" * 200, working_page_ids=[])
    mocker.patch(
        "rumil.calls.impact_filtered_context.bfs_evidence_pages_within_distance",
        new_callable=mocker.AsyncMock,
        return_value=[],
    )
    wrapper = ImpactFilteredContext(inner_builder=_StubInnerBuilder(inner))
    infra = _make_infra(tmp_db, view_call, question_page)
    record_spy = mocker.patch.object(infra.trace, "record", new_callable=mocker.AsyncMock)
    with override_settings(
        rumil_smoke_test="",
        impact_filter_pare_threshold_tokens=1_000,
    ):
        await wrapper.build_context(infra)

    impact_events = [
        c.args[0]
        for c in record_spy.call_args_list
        if getattr(c.args[0], "event", None) == "impact_filter"
    ]
    assert len(impact_events) == 1
    assert impact_events[0].paring_triggered is False
