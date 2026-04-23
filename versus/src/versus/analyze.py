"""Analysis: gen-model × judge-model matrix of %-picks-human per condition."""

from __future__ import annotations

import collections
import pathlib

from versus import jsonl

HUMAN = "human"


def _strip_prefix(source_id: str) -> tuple[str, str]:
    """Return (condition, model_id). condition in {completion, paraphrase, human}."""
    if source_id == HUMAN:
        return "human", HUMAN
    if source_id.startswith("paraphrase:"):
        return "paraphrase", source_id.split(":", 1)[1]
    return "completion", source_id


def content_test_matrix(judgments_log: pathlib.Path) -> dict:
    """Compute `%-picks-J's-paraphrase` per (gen_model, judge_model, criterion).

    Counts pairs of the form (paraphrase:J, completion:G) where the judge J is
    also the paraphrase author. Semantically: "does J prefer G's completion, or
    the human content rewritten in J's own voice?" Style is controlled at J on
    the diagonal (G == J); off-diagonal mixes style + content.

    Cell value is fraction of J-picks-paraphrase:J (ties = 0.5).
    """
    counts: dict[tuple[str, str, str], list[float]] = collections.defaultdict(lambda: [0.0, 0])
    for row in jsonl.read(judgments_log):
        if row.get("verdict") is None:
            continue
        if row.get("contamination_note"):
            continue
        j = row["judge_model"]
        baseline = f"paraphrase:{j}"
        a, b = row["source_a"], row["source_b"]
        if baseline not in (a, b):
            continue
        other = b if a == baseline else a
        if other == HUMAN or other.startswith("paraphrase:"):
            continue
        gen_model = other
        key = (gen_model, j, row["criterion"])
        counts[key][1] += 1
        if row["winner_source"] == baseline:
            counts[key][0] += 1.0
        elif row["winner_source"] == "tie":
            counts[key][0] += 0.5
    return {k: (h / t, int(t)) for k, (h, t) in counts.items()}


def matrix(
    judgments_log: pathlib.Path,
    criterion: str | None = None,
    include_contaminated: bool = False,
) -> dict:
    """Compute %-picks-human per (gen_model, judge_model, condition).

    Only counts pairs where one side is human. Ties count as 0.5 for human.

    ``include_contaminated``: by default, rows tagged with a
    ``contamination_note`` field are excluded from the aggregate
    (produced before a blind-judge leak fix; the verdict doesn't
    measure what the matrix claims to measure). Pass True to include
    them and see the pre-fix distribution.
    """
    # counts[(gen_model, judge_model, condition, criterion)] = [human_points, total]
    counts: dict[tuple[str, str, str, str], list[float]] = collections.defaultdict(lambda: [0.0, 0])
    for row in jsonl.read(judgments_log):
        if row.get("verdict") is None:
            continue
        if not include_contaminated and row.get("contamination_note"):
            continue
        if criterion is not None and row.get("criterion") != criterion:
            continue
        a, b = row["source_a"], row["source_b"]
        if a != HUMAN and b != HUMAN:
            continue
        other = b if a == HUMAN else a
        cond, gen_model = _strip_prefix(other)
        judge_model = row["judge_model"]
        crit = row["criterion"]
        key = (gen_model, judge_model, cond, crit)
        counts[key][1] += 1
        if row["winner_source"] == HUMAN:
            counts[key][0] += 1.0
        elif row["winner_source"] == "tie":
            counts[key][0] += 0.5
    return {k: (h / t, int(t)) for k, (h, t) in counts.items()}


def format_matrix(
    data: dict,
    gen_models: list[str] | None = None,
    judge_models: list[str] | None = None,
    condition: str = "completion",
    criterion: str | None = None,
) -> str:
    """Render as a text table. If criterion is None, averages across criteria."""
    if not gen_models:
        gen_models = sorted({k[0] for k in data if k[2] == condition})
    if not judge_models:
        judge_models = sorted({k[1] for k in data if k[2] == condition})

    # aggregate over criteria if needed
    cell: dict[tuple[str, str], tuple[float, int]] = {}
    for g in gen_models:
        for j in judge_models:
            hs, ns = 0.0, 0
            for (gg, jj, cc, crit), (pct, n) in data.items():
                if gg == g and jj == j and cc == condition:
                    if criterion and crit != criterion:
                        continue
                    hs += pct * n
                    ns += n
            cell[(g, j)] = ((hs / ns) if ns else 0.0, ns)

    col_w = max([len(j) for j in judge_models] + [10])
    gen_w = max([len(g) for g in gen_models] + [10])
    lines = []
    header_label = f"condition={condition}" + (
        f"  criterion={criterion}" if criterion else "  (avg over criteria)"
    )
    lines.append(header_label)
    lines.append("")
    header = " " * gen_w + " | " + " | ".join(j.rjust(col_w) for j in judge_models)
    lines.append(header)
    lines.append("-" * len(header))
    for g in gen_models:
        row = (
            g.ljust(gen_w)
            + " | "
            + " | ".join(
                (
                    f"{cell[(g, j)][0] * 100:5.1f}% ({cell[(g, j)][1]})".rjust(col_w)
                    if cell[(g, j)][1]
                    else "  -  ".rjust(col_w)
                )
                for j in judge_models
            )
        )
        lines.append(row)
    return "\n".join(lines)
