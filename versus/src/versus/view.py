"""Presentation and aggregation logic for the ``/versus/results`` UI.

Computes cells, matrices, and condition metadata from the raw judgment /
completion stores. The rumil FastAPI router
(``src/rumil/api/versus_router.py``) is a thin shell that wraps these
results in typed pydantic envelopes; any edits that affect the JSON
response shape should be reflected in ``router`` pydantic models and
vice versa.

Returns plain dicts / dataclasses so callers (the router, ad-hoc
scripts) decide how to serialise. No FastAPI / pydantic imports here.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from versus import analyze as versus_analyze


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
