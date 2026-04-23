"""Analysis: gen-model × judge-model matrix of %-picks-human per condition."""

from __future__ import annotations

import collections
import pathlib
import re

from versus import jsonl

HUMAN = "human"


def model_sort_key(judge: str) -> tuple:
    """Order model ids (gen or judge) by family -> weak->strong -> variant.

    Families (left to right): gemini, openai, anthropic, other. Weak->strong
    within family: flash < pro for gemini, nano < mini < full for openai,
    haiku < sonnet < opus for anthropic. Variant (relevant for judges):
    bare openrouter < anthropic: (legacy text) < rumil:text < rumil:ws <
    rumil:orch. Human judges pinned to the end.
    """
    low = judge.lower()

    if low.startswith("human:"):
        return (99, 99, "", 99, judge)

    if low.startswith("rumil:orch:"):
        variant = 4
        base = judge.split(":")[2] if len(judge.split(":")) >= 3 else judge
    elif low.startswith("rumil:ws:"):
        variant = 3
        base = judge.split(":")[2] if len(judge.split(":")) >= 3 else judge
    elif low.startswith("rumil:text:"):
        variant = 2
        base = judge.split(":")[2] if len(judge.split(":")) >= 3 else judge
    elif low.startswith("anthropic:"):
        variant = 1
        base = judge.split(":", 1)[1]
    elif "/" in judge:
        variant = 0
        base = judge.split("/", 1)[1]
    else:
        variant = 0
        base = judge
    base_low = base.lower()

    if "gemini" in base_low or low.startswith("google/"):
        family = 0
        if "flash-lite" in base_low:
            strength = 0
        elif "flash" in base_low:
            strength = 1
        elif "pro" in base_low:
            strength = 2
        else:
            strength = 5
    elif "gpt" in base_low or low.startswith("openai/"):
        family = 1
        if "nano" in base_low:
            strength = 0
        elif "mini" in base_low:
            strength = 1
        else:
            strength = 2
    elif (
        "claude" in base_low
        or "haiku" in base_low
        or "sonnet" in base_low
        or "opus" in base_low
        or low.startswith("anthropic")
        or low.startswith("rumil:")
    ):
        family = 2
        if "haiku" in base_low:
            strength = 0
        elif "sonnet" in base_low:
            strength = 1
        elif "opus" in base_low:
            strength = 2
        else:
            strength = 5
    else:
        family = 3
        strength = 5

    return (family, strength, base_low, variant, judge)


_PROMPT_HASH_RE = re.compile(r"^p[0-9a-f]{8}$")


def judge_label(judge: str) -> dict:
    """Break a judge_model id into stacked header parts.

    Returns {variant, model, task, phash}. `phash` is the `:p<sha8>` prompt
    version suffix (present on post-hash rumil judgments; absent on legacy
    pre-hash data).
    """
    if judge.startswith("human:"):
        return {"variant": "human", "model": judge.split(":", 1)[1], "task": None, "phash": None}
    if (
        judge.startswith("rumil:orch:")
        or judge.startswith("rumil:ws:")
        or judge.startswith("rumil:text:")
    ):
        parts = judge.split(":")
        phash = None
        if parts and _PROMPT_HASH_RE.match(parts[-1]):
            phash = parts[-1]
            parts = parts[:-1]
        variant = f"{parts[0]}:{parts[1]}"
        model = parts[2] if len(parts) >= 3 else judge
        if parts[1] == "text":
            tail = parts[3:]
        else:
            tail = parts[4:]  # skip ws_short hash
            if tail and tail[0].startswith("b") and tail[0][1:].isdigit():
                variant = f"{variant} {tail[0]}"
                tail = tail[1:]
        task = ":".join(tail) if tail else None
        return {"variant": variant, "model": model, "task": task, "phash": phash}
    if judge.startswith("anthropic:"):
        return {
            "variant": "anthropic",
            "model": judge.split(":", 1)[1],
            "task": None,
            "phash": None,
        }
    if "/" in judge:
        provider, model = judge.split("/", 1)
        return {"variant": provider, "model": model, "task": None, "phash": None}
    return {"variant": None, "model": judge, "task": None, "phash": None}


def cell_color(pct: float) -> str:
    """Gradient: 0 -> orange (model preferred), 50 -> light gray, 100 -> green (human preferred)."""
    if pct <= 0.5:
        t = pct / 0.5
        r, g, b = 255, int(111 + (238 - 111) * t), int(67 + (238 - 67) * t)
    else:
        t = (pct - 0.5) / 0.5
        r = int(238 - (238 - 110) * t)
        g = int(238 - (238 - 199) * t)
        b = int(238 - (238 - 120) * t)
    return f"rgb({r},{g},{b})"


def text_color(pct: float) -> str:
    return "#111"


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
