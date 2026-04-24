"""Presentation and aggregation logic for the ``/versus/results`` UI.

Computes cells, matrices, and condition metadata from the raw judgment /
completion stores. Pairs enumeration for the human-judging UI lives here
too. The rumil FastAPI router (``src/rumil/api/versus_router.py``) is a
thin shell that wraps these results in typed pydantic envelopes; any
edits that affect the JSON response shape should be reflected in
``router`` pydantic models and vice versa.

Returns plain dicts / dataclasses so callers (the router, ad-hoc
scripts) decide how to serialise. No FastAPI / pydantic imports here.
"""

from __future__ import annotations

import itertools
import json
import math
import pathlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from versus import analyze as versus_analyze
from versus import config as versus_config
from versus import judge as versus_judge


@dataclass(frozen=True)
class ConditionMeta:
    title: str
    pair: str
    cell_meaning: str
    value_picks: str


COND_META: dict[str, ConditionMeta] = {
    "completion": ConditionMeta(
        title="vs human · from-scratch continuation",
        pair="pair: (human continuation, G's from-scratch continuation) — judged by J",
        cell_meaning="cell: % J picks human. High → J prefers the real continuation over G's new one.",
        value_picks="human",
    ),
    "paraphrase": ConditionMeta(
        title="vs human · same-model paraphrase",
        pair="pair: (human continuation, G's rewrite of the human continuation) — judged by J",
        cell_meaning="cell: % J picks human. Content is held constant; this isolates style preference.",
        value_picks="human",
    ),
    "content-test": ConditionMeta(
        title="style-controlled · content test",
        pair="pair: (G's from-scratch continuation, J's rewrite of the human) — judged by J",
        cell_meaning=(
            "cell: % J picks its own human-content-baseline. On the diagonal (G=J), style is held"
            " at J; off-diagonal mixes styles."
        ),
        value_picks="J's paraphrase (= human content in J's voice)",
    ),
}


@dataclass(frozen=True)
class Cell:
    pct: float | None
    n: int
    wins: int
    ties: int
    losses: int
    tie_frac: float | None
    ci_lo: float | None
    ci_hi: float | None
    bg: str
    fg: str


@dataclass(frozen=True)
class GenJudgeCell:
    gen_model: str
    judge_model: str
    cell: Cell


def _wilson_ci(wins_eq: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval. ``wins_eq`` treats ties as 0.5 wins, matching
    how ``pct`` is computed so the CI is self-consistent with the cell's
    rendered number. ``n > 0`` is required by the caller."""
    p = wins_eq / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    spread = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def build_cell(
    data: dict,
    gen: str,
    jmod: str,
    cond: str,
    crit_filter: str | None,
    *,
    keyed_by_condition: bool,
) -> Cell:
    """Aggregate ``data`` counts for one (gen, judge, condition[, crit]) cell.

    ``data`` is the mapping returned by :func:`versus.analyze.matrix` /
    :func:`versus.analyze.content_test_matrix` — values are
    ``(pct, n, wins, ties, losses)``. For the main matrices the keys are
    4-tuples ``(gen, judge, condition, criterion)`` and we filter on
    ``condition``; for the content-test matrix the keys are 3-tuples
    ``(gen, judge, criterion)`` with ``condition`` baked in, so
    ``keyed_by_condition=False`` skips the condition check.
    """
    wins_total = 0
    ties_total = 0
    losses_total = 0
    for k, row in data.items():
        if keyed_by_condition:
            g, j, c, cr = k
            if g != gen or j != jmod or c != cond:
                continue
        else:
            g, j, cr = k
            if g != gen or j != jmod:
                continue
        if crit_filter and cr != crit_filter:
            continue
        wins_total += row[2]
        ties_total += row[3]
        losses_total += row[4]
    n = wins_total + ties_total + losses_total
    if n == 0:
        return Cell(
            pct=None,
            n=0,
            wins=0,
            ties=0,
            losses=0,
            tie_frac=None,
            ci_lo=None,
            ci_hi=None,
            bg="#f4f4f0",
            fg="#999",
        )
    wins_eq = wins_total + 0.5 * ties_total
    pct = wins_eq / n
    ci_lo, ci_hi = _wilson_ci(wins_eq, n)
    return Cell(
        pct=pct,
        n=n,
        wins=wins_total,
        ties=ties_total,
        losses=losses_total,
        tie_frac=ties_total / n,
        ci_lo=ci_lo,
        ci_hi=ci_hi,
        bg=versus_analyze.cell_color(pct),
        fg=versus_analyze.text_color(pct),
    )


def matrix_cells(
    data: dict,
    gen_models: Sequence[str],
    judge_models: Sequence[str],
    cond: str,
    crit: str | None,
    *,
    keyed_by_condition: bool,
) -> list[GenJudgeCell]:
    return [
        GenJudgeCell(
            gen_model=g,
            judge_model=j,
            cell=build_cell(data, g, j, cond, crit, keyed_by_condition=keyed_by_condition),
        )
        for g in gen_models
        for j in judge_models
    ]


@dataclass(frozen=True)
class PairShape:
    """Blind-judging pair payload, in display order, for the human judging UI.

    ``a`` / ``b`` are the raw source_ids (alphabetical); ``first`` / ``second``
    are those same ids re-ordered by :func:`versus.judge.order_pair` so every
    judge sees the same A/B assignment for the same essay+pair.
    """

    essay_id: str
    prefix_hash: str
    a: str
    b: str
    first_source: str
    second_source: str
    first_text: str
    second_text: str
    prefix_text: str
    title: str


def enumerate_pairs(
    cfg: versus_config.Config,
    completions_log: pathlib.Path,
    essay_paths: Sequence[pathlib.Path],
) -> Iterator[PairShape]:
    """Yield every blind pair derivable from the completions store.

    Pair enumeration mirrors :func:`versus.judge.run` -- same filtering
    (skip groups with <2 contestants, honor ``include_human_as_contestant``)
    and same deterministic display ordering. ``essay_paths`` are the cached
    essay JSONs; the caller passes them in so this function stays pure (no
    filesystem walking beyond reading those specific paths).
    """
    groups, prefix_texts = versus_judge.load_sources_by_essay(completions_log)
    titles: dict[str, str] = {}
    for p in essay_paths:
        with open(p) as f:
            d = json.load(f)
        titles[d["id"]] = d["title"]

    for (essay_id, prefix_hash), sources in groups.items():
        source_ids = sorted(sources.keys())
        if not cfg.judging.include_human_as_contestant:
            source_ids = [s for s in source_ids if s != "human"]
        if len(source_ids) < 2:
            continue
        prefix_text = prefix_texts.get((essay_id, prefix_hash), "")
        for a_id, b_id in itertools.combinations(source_ids, 2):
            src_a = versus_judge.Source(a_id, sources[a_id])
            src_b = versus_judge.Source(b_id, sources[b_id])
            first, second = versus_judge.order_pair(essay_id, src_a, src_b)
            yield PairShape(
                essay_id=essay_id,
                prefix_hash=prefix_hash,
                a=a_id,
                b=b_id,
                first_source=first.source_id,
                second_source=second.source_id,
                first_text=first.text,
                second_text=second.text,
                prefix_text=prefix_text,
                title=titles.get(essay_id, essay_id),
            )


def pair_as_dict(pair: PairShape) -> dict[str, Any]:
    """Adapter: PairShape -> the dict shape the frontend NextPair expects.

    Kept as a function (not ``dataclasses.asdict``) so callers can add
    criterion/criteria_desc/progress fields without reconstructing.
    """
    return {
        "essay_id": pair.essay_id,
        "prefix_hash": pair.prefix_hash,
        "a": pair.a,
        "b": pair.b,
        "first_source": pair.first_source,
        "second_source": pair.second_source,
        "first_text": pair.first_text,
        "second_text": pair.second_text,
        "prefix_text": pair.prefix_text,
        "title": pair.title,
    }
