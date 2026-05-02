"""Tests for the context-eval API diff computation."""

from rumil.api.app import _diff_context_pages
from rumil.api.schemas import ContextBuiltEventOut
from rumil.tracing.trace_events import PageRef


def _arm(
    *,
    working: list[tuple[str, str]] | None = None,
    preloaded: list[tuple[str, str]] | None = None,
    scope_linked: list[tuple[str, str]] | None = None,
) -> ContextBuiltEventOut:
    """Build a minimal ContextBuiltEventOut from (id, headline) tuples."""

    def _refs(pairs: list[tuple[str, str]] | None) -> list[PageRef]:
        return [PageRef(id=i, headline=h) for i, h in (pairs or [])]

    return ContextBuiltEventOut(
        ts="2026-01-01T00:00:00Z",
        call_id="test",
        working_context_page_ids=_refs(working),
        preloaded_page_ids=_refs(preloaded),
        scope_linked_pages=_refs(scope_linked),
    )


def test_diff_disjoint_arms_buckets_everything():
    gold = _arm(working=[("a", "alpha"), ("b", "beta")])
    cand = _arm(working=[("c", "gamma"), ("d", "delta")])
    only_gold, only_cand, both = _diff_context_pages(gold, cand)
    assert sorted(p.id for p in only_gold) == ["a", "b"]
    assert sorted(p.id for p in only_cand) == ["c", "d"]
    assert both == []


def test_diff_full_overlap_lands_in_both():
    gold = _arm(working=[("a", "alpha"), ("b", "beta")])
    cand = _arm(working=[("a", "alpha"), ("b", "beta")])
    only_gold, only_cand, both = _diff_context_pages(gold, cand)
    assert only_gold == []
    assert only_cand == []
    assert sorted(p.id for p in both) == ["a", "b"]


def test_diff_unions_working_preloaded_and_scope_linked():
    gold = _arm(
        working=[("a", "alpha")],
        preloaded=[("p1", "pre")],
        scope_linked=[("s1", "scope")],
    )
    cand = _arm(
        working=[("a", "alpha")],
        preloaded=[("p2", "pre2")],
        scope_linked=[("s1", "scope")],
    )
    only_gold, only_cand, both = _diff_context_pages(gold, cand)
    only_gold_ids = sorted(p.id for p in only_gold)
    only_cand_ids = sorted(p.id for p in only_cand)
    both_ids = sorted(p.id for p in both)

    # p1 is preloaded in gold only; p2 preloaded in candidate only.
    assert only_gold_ids == ["p1"]
    assert only_cand_ids == ["p2"]
    # Both arms see "a" (working) and "s1" (scope-linked).
    assert both_ids == ["a", "s1"]


def test_diff_preserves_headlines_from_owning_arm():
    gold = _arm(working=[("a", "gold-headline")])
    cand = _arm(working=[("b", "candidate-headline")])
    only_gold, only_cand, _ = _diff_context_pages(gold, cand)
    assert only_gold[0].headline == "gold-headline"
    assert only_cand[0].headline == "candidate-headline"


def test_diff_dedupes_within_arm_when_id_appears_in_multiple_buckets():
    """Same id in working AND scope_linked of one arm shouldn't land in 'both'
    just because the other arm has it in any bucket — and shouldn't appear
    twice anywhere."""
    gold = _arm(
        working=[("a", "alpha")],
        scope_linked=[("a", "alpha")],
    )
    cand = _arm(working=[("a", "alpha")])
    only_gold, only_cand, both = _diff_context_pages(gold, cand)
    assert only_gold == []
    assert only_cand == []
    assert [p.id for p in both] == ["a"]
