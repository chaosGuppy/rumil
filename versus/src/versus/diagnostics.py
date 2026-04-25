"""Judge-bias / small-n / per-essay sanity diagnostics over the judgments log.

Pure functions over already-loaded judgment rows. The API layer handles IO
and filter plumbing; this module just aggregates.

Three analyses:
  - judge_bias_rows: per judge_model_base, all-rows A% vs completion-vs-
    completion A% (both with Wilson 95% CIs). Gap is the content-bias
    estimate (i.e. how much having the human on one side shifts votes
    beyond pure position bias).
  - small_n_cells: (gen, judge, condition, criterion) tuples with n<5.
  - essay_flags: per essay, tie rate + source sweep detection.
"""

from __future__ import annotations

import collections
import math
from collections.abc import Iterable, Sequence

from versus import analyze

HUMAN = "human"


def judge_base_key(judge_model: str) -> str:
    """Collapse judge_model ids to a base grouping key.

    Strips the :p<hash>:v<N> suffix and (for rumil variants) the ws-short
    hash / task / budget tails so two prompt-versions of the same judge
    share a row. Mirrors JudgeHeader with includeTask=false — base is
    variant + model, no task / no phash.
    """
    label = analyze.judge_label(judge_model)
    variant = label["variant"]
    model = label["model"]
    # variant carries things like "rumil:orch b4 v2"; strip the trailing
    # version tag so v1 and v2 collapse into the same base row. Budget tag
    # (b4) is judge-behaviour-relevant so we keep it.
    if variant:
        parts = variant.split(" ")
        parts = [p for p in parts if not (p.startswith("v") and p[1:].isdigit())]
        variant = " ".join(parts)
    return f"{variant}:{model}" if variant else model


def wilson_interval(successes: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion. Handles fractional
    successes (we treat ties as 0.5 successes, which is a mild abuse but
    matches the rest of the analysis pipeline)."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def _a_preference(row: dict) -> float | None:
    """Return 1.0 if judge picked A (= display_first), 0.0 if B, 0.5 for tie.

    A verdict maps to `display_first` / `display_second`. We use the
    display-side mapping because that's the slot the judge sees as
    "A"/"B"; the raw source_a/source_b is just pair-ordering state.
    """
    verdict = row.get("verdict")
    if verdict is None:
        return None
    winner = row.get("winner_source")
    first = row.get("display_first")
    second = row.get("display_second")
    if winner == "tie":
        return 0.5
    if winner == first:
        return 1.0
    if winner == second:
        return 0.0
    return None


def _is_completion_vs_completion(row: dict) -> bool:
    """True iff neither side is human (pure A/B position test).

    Paraphrase-rewrites of the human continuation count as non-human —
    their content leaks human material, but the *displayed* source id
    doesn't, which is what position-bias measurement needs.
    """
    a = row.get("source_a")
    b = row.get("source_b")
    return a != HUMAN and b != HUMAN


class JudgeBiasRow:
    """Per-judge aggregate. Not a pydantic model — API layer wraps it."""

    __slots__ = (
        "all_a_rate",
        "all_ci_hi",
        "all_ci_lo",
        "content_bias",
        "cvc_a_rate",
        "cvc_ci_hi",
        "cvc_ci_lo",
        "judge_base",
        "n_cvc",
        "n_total",
    )

    def __init__(
        self,
        judge_base: str,
        n_total: int,
        all_a_rate: float | None,
        all_ci_lo: float | None,
        all_ci_hi: float | None,
        n_cvc: int,
        cvc_a_rate: float | None,
        cvc_ci_lo: float | None,
        cvc_ci_hi: float | None,
        content_bias: float | None,
    ):
        self.judge_base = judge_base
        self.n_total = n_total
        self.all_a_rate = all_a_rate
        self.all_ci_lo = all_ci_lo
        self.all_ci_hi = all_ci_hi
        self.n_cvc = n_cvc
        self.cvc_a_rate = cvc_a_rate
        self.cvc_ci_lo = cvc_ci_lo
        self.cvc_ci_hi = cvc_ci_hi
        self.content_bias = content_bias


_CVC_MIN_N = 20


def judge_bias_rows(rows: Iterable[dict]) -> list[JudgeBiasRow]:
    """Compute per-judge A-preference rates and content-bias gap.

    Groups rows by judge_base_key, then splits each group into all-rows
    and completion-vs-completion subsets. Sorted by |all-A% − 50|
    descending so the most biased judges surface first.
    """
    all_buckets: dict[str, list[float]] = collections.defaultdict(list)
    cvc_buckets: dict[str, list[float]] = collections.defaultdict(list)
    for row in rows:
        a_pref = _a_preference(row)
        if a_pref is None:
            continue
        base = judge_base_key(row.get("judge_model", ""))
        all_buckets[base].append(a_pref)
        if _is_completion_vs_completion(row):
            cvc_buckets[base].append(a_pref)

    out: list[JudgeBiasRow] = []
    for base, prefs in all_buckets.items():
        n_all = len(prefs)
        if n_all == 0:
            continue
        all_rate = sum(prefs) / n_all
        all_lo, all_hi = wilson_interval(sum(prefs), n_all)

        cvc_prefs = cvc_buckets.get(base, [])
        n_cvc = len(cvc_prefs)
        if n_cvc >= _CVC_MIN_N:
            cvc_rate = sum(cvc_prefs) / n_cvc
            cvc_lo, cvc_hi = wilson_interval(sum(cvc_prefs), n_cvc)
            bias = all_rate - cvc_rate
        else:
            cvc_rate = None
            cvc_lo = None
            cvc_hi = None
            bias = None

        out.append(
            JudgeBiasRow(
                judge_base=base,
                n_total=n_all,
                all_a_rate=all_rate,
                all_ci_lo=all_lo,
                all_ci_hi=all_hi,
                n_cvc=n_cvc,
                cvc_a_rate=cvc_rate,
                cvc_ci_lo=cvc_lo,
                cvc_ci_hi=cvc_hi,
                content_bias=bias,
            )
        )

    out.sort(key=lambda r: abs((r.all_a_rate or 0.5) - 0.5), reverse=True)
    return out


class SmallNCell:
    __slots__ = ("condition", "criterion", "gen_model", "judge_base", "n")

    def __init__(self, gen_model: str, judge_base: str, condition: str, criterion: str, n: int):
        self.gen_model = gen_model
        self.judge_base = judge_base
        self.condition = condition
        self.criterion = criterion
        self.n = n


_SMALL_N_THRESHOLD = 5


def small_n_cells(rows: Iterable[dict]) -> list[SmallNCell]:
    """Scan matrix cells (gen × judge × condition × criterion), list n<5.

    Only counts human-vs-model rows (the same scope as matrix()); cvc
    pairs don't surface here because they aren't part of the matrix that
    drives conclusions. Sorted by n ascending.
    """
    counts: dict[tuple[str, str, str, str], int] = collections.defaultdict(int)
    for row in rows:
        if row.get("verdict") is None:
            continue
        a = row.get("source_a", "")
        b = row.get("source_b", "")
        if a != HUMAN and b != HUMAN:
            continue
        other = b if a == HUMAN else a
        cond, gen_model = analyze._strip_prefix(other)
        if cond == "human":
            continue
        base = judge_base_key(row.get("judge_model", ""))
        key = (gen_model, base, cond, row.get("criterion", ""))
        counts[key] += 1
    out = [
        SmallNCell(gen_model=g, judge_base=j, condition=c, criterion=cr, n=n)
        for (g, j, c, cr), n in counts.items()
        if n < _SMALL_N_THRESHOLD
    ]
    out.sort(key=lambda c: (c.n, c.gen_model, c.judge_base, c.condition, c.criterion))
    return out


class EssayFlag:
    __slots__ = (
        "essay_id",
        "n_judgments",
        "sweep_n",
        "sweep_source",
        "tie_flag",
        "tie_rate",
    )

    def __init__(
        self,
        essay_id: str,
        n_judgments: int,
        tie_rate: float,
        tie_flag: bool,
        sweep_source: str | None,
        sweep_n: int,
    ):
        self.essay_id = essay_id
        self.n_judgments = n_judgments
        self.tie_rate = tie_rate
        self.tie_flag = tie_flag
        self.sweep_source = sweep_source
        self.sweep_n = sweep_n


_TIE_RATE_THRESHOLD = 0.10


def essay_flags(rows: Iterable[dict]) -> list[EssayFlag]:
    """Per-essay sanity checks: tie-rate high, or a source sweeping all pairs.

    Sweep detection: for each essay, per source_id, count the pairs it
    appeared in across all judges and the fraction it won (tie = 0.5).
    A source with n >= 2 appearances and a 100%-win rate is flagged —
    legitimate dominance is possible but worth a look (prefix/content
    leak is the failure mode to watch). Only returns flagged essays.
    """
    by_essay: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        if row.get("verdict") is None:
            continue
        by_essay[row.get("essay_id", "")].append(row)

    out: list[EssayFlag] = []
    for eid, erows in by_essay.items():
        n = len(erows)
        if n == 0:
            continue
        ties = sum(1 for r in erows if r.get("winner_source") == "tie")
        tie_rate = ties / n
        tie_flag = tie_rate > _TIE_RATE_THRESHOLD

        source_appearances: dict[str, int] = collections.defaultdict(int)
        source_wins: dict[str, float] = collections.defaultdict(float)
        for r in erows:
            a = r.get("source_a", "")
            b = r.get("source_b", "")
            w = r.get("winner_source")
            source_appearances[a] += 1
            source_appearances[b] += 1
            if w == "tie":
                source_wins[a] += 0.5
                source_wins[b] += 0.5
            elif w == a:
                source_wins[a] += 1.0
            elif w == b:
                source_wins[b] += 1.0

        sweep_source: str | None = None
        sweep_n = 0
        for sid, appear in source_appearances.items():
            if appear >= 2 and source_wins[sid] == appear:
                sweep_source = sid
                sweep_n = appear
                break

        if tie_flag or sweep_source is not None:
            out.append(
                EssayFlag(
                    essay_id=eid,
                    n_judgments=n,
                    tie_rate=tie_rate,
                    tie_flag=tie_flag,
                    sweep_source=sweep_source,
                    sweep_n=sweep_n,
                )
            )

    out.sort(key=lambda f: (not f.tie_flag, f.essay_id))
    return out


def biased_judge_count(bias_rows: Sequence[JudgeBiasRow], threshold_pp: float = 5.0) -> int:
    """Count judges whose all-rows A% deviates from 50% by more than threshold."""
    t = threshold_pp / 100.0
    return sum(1 for r in bias_rows if r.all_a_rate is not None and abs(r.all_a_rate - 0.5) > t)
