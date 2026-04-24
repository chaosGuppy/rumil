"""Analysis: gen-model × judge-model matrix of %-picks-human per condition."""

from __future__ import annotations

import collections
import pathlib
import re

from versus import jsonl, judge

HUMAN = "human"


def _content_test_baseline(judge_model: str) -> str:
    """Compose the paraphrase source_id that matches this judge's model.

    Paraphrases are authored via OpenRouter and keyed as
    ``paraphrase:<openrouter_model_id>`` (e.g. ``paraphrase:openai/gpt-5.4``,
    slash). Anthropic-direct judges use the ``anthropic:<model>`` form (colon).
    Normalize the latter so the baseline lookup hits a paraphrase row when the
    same underlying model authored one.
    """
    base = judge.base_judge_model(judge_model)
    if base.startswith("anthropic:"):
        base = "anthropic/" + base.removeprefix("anthropic:")
    return f"paraphrase:{base}"


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
_VERSION_TAG_RE = re.compile(r"^v\d+$")
_SAMPLING_HASH_RE = re.compile(r"^s[0-9a-f]{8}$")
_TOOL_HASH_RE = re.compile(r"^t[0-9a-f]{8}$")
_SURFACE_HASH_RE = re.compile(r"^q[0-9a-f]{8}$")


def _peel_ts_tag(parts: list[str]) -> list[str]:
    while parts and (
        _SAMPLING_HASH_RE.match(parts[-1])
        or _TOOL_HASH_RE.match(parts[-1])
        or _SURFACE_HASH_RE.match(parts[-1])
    ):
        parts = parts[:-1]
    return parts


def _strip_phash_version(parts: list[str]) -> tuple[list[str], str | None, str | None]:
    """Peel trailing ``:s<hash>``, ``:t<hash>``, ``:q<hash>``, ``:v<N>``, then ``:p<hash>`` off a split judge_model.

    The sampling / tool-prompt / pair-surface hashes are absorbed
    silently (not returned) because the frontend doesn't render them
    -- they exist only for dedup-key discipline. The human-readable
    sampling dict lives on the judgment row; the tool-prompt text
    lives in-repo under ``src/rumil/workspace_exploration/``; the pair
    surface fork is automatic via ``compute_pair_surface_hash()``.
    ``:t``, ``:s``, and ``:q`` peel symmetrically so any trailing
    ordering is tolerated.
    """
    parts = _peel_ts_tag(parts)
    version = None
    if parts and _VERSION_TAG_RE.match(parts[-1]):
        version = parts[-1]
        parts = parts[:-1]
    phash = None
    if parts and _PROMPT_HASH_RE.match(parts[-1]):
        phash = parts[-1]
        parts = parts[:-1]
    return parts, phash, version


def judge_label(judge: str) -> dict:
    """Break a judge_model id into stacked header parts.

    Returns {variant, model, task, phash}. `phash` is the `:p<sha8>` prompt
    version suffix (present on post-hash judgments; absent on legacy
    pre-hash data). A `:v<N>` version suffix (after the phash) is folded
    into `variant` so the phash remains separable. Applies to all
    variants: rumil:*, anthropic:*, and OpenRouter bare-provider form.
    """
    if judge.startswith("human:"):
        return {"variant": "human", "model": judge.split(":", 1)[1], "task": None, "phash": None}
    if (
        judge.startswith("rumil:orch:")
        or judge.startswith("rumil:ws:")
        or judge.startswith("rumil:text:")
    ):
        parts, phash, version = _strip_phash_version(judge.split(":"))
        variant = f"{parts[0]}:{parts[1]}"
        model = parts[2] if len(parts) >= 3 else judge
        if parts[1] == "text":
            tail = parts[3:]
        else:
            tail = parts[4:]  # skip ws_short hash
            if tail and tail[0].startswith("b") and tail[0][1:].isdigit():
                variant = f"{variant} {tail[0]}"
                tail = tail[1:]
        if version:
            variant = f"{variant} {version}"
        task = ":".join(tail) if tail else None
        return {"variant": variant, "model": model, "task": task, "phash": phash}
    if judge.startswith("anthropic:"):
        parts, phash, version = _strip_phash_version(judge.split(":"))
        model = ":".join(parts[1:]) if len(parts) > 1 else judge
        variant = f"anthropic {version}" if version else "anthropic"
        return {"variant": variant, "model": model, "task": None, "phash": phash}
    if "/" in judge:
        parts, phash, version = _strip_phash_version(judge.split(":"))
        base = ":".join(parts)
        provider, model = base.split("/", 1)
        variant = f"{provider} {version}" if version else provider
        return {"variant": variant, "model": model, "task": None, "phash": phash}
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


def _is_stale_row(row: dict, current_prefix_hashes: dict[str, str] | None) -> bool:
    """True iff the row cannot be verified as matching the current essay cache.

    A row is stale when either:
      * its ``essay_id`` isn't in the current cache (essay was removed or
        renamed — e.g. forethought essays migrated to the ``forethought__``
        namespace no longer match their old jsonl rows), or
      * its ``prefix_config_hash`` doesn't match the current hash for that
        essay (essay content or prefix params changed).

    Returns False (not stale) only when we pass both checks. When
    ``current_prefix_hashes`` is None (caller doesn't want staleness
    filtering) we defer and return False.
    """
    if current_prefix_hashes is None:
        return False
    eid = row.get("essay_id")
    if eid not in current_prefix_hashes:
        return True
    return row.get("prefix_config_hash") != current_prefix_hashes[eid]


def content_test_matrix(
    judgments_log: pathlib.Path,
    include_contaminated: bool = False,
    current_prefix_hashes: dict[str, str] | None = None,
    include_stale: bool = True,
) -> dict:
    """Compute `%-picks-J's-paraphrase` per (gen_model, judge_model, criterion).

    Counts pairs of the form (paraphrase:J, completion:G) where the judge J is
    also the paraphrase author. Semantically: "does J prefer G's completion, or
    the human content rewritten in J's own voice?" Style is controlled at J on
    the diagonal (G == J); off-diagonal mixes style + content.

    Cell value is fraction of J-picks-paraphrase:J (ties = 0.5).

    ``include_contaminated`` mirrors ``matrix()``: rows tagged with a
    ``contamination_note`` are excluded by default; pass True to see the
    pre-fix distribution.

    Returns ``{key: (pct, n, wins, ties, losses)}`` where ``wins`` counts the
    judge picking the paraphrase-baseline, ``losses`` counts it picking the
    other side, and ``ties`` counts explicit ties. ``pct`` treats ties as 0.5.
    """
    # counts[key] = [wins, ties, losses]
    counts: dict[tuple[str, str, str], list[int]] = collections.defaultdict(lambda: [0, 0, 0])
    for row in jsonl.read_dedup(judgments_log):
        if row.get("verdict") is None:
            continue
        if not include_contaminated and row.get("contamination_note"):
            continue
        if not include_stale and _is_stale_row(row, current_prefix_hashes):
            continue
        j = row["judge_model"]
        baseline = _content_test_baseline(j)
        a, b = row["source_a"], row["source_b"]
        if baseline not in (a, b):
            continue
        other = b if a == baseline else a
        if other == HUMAN or other.startswith("paraphrase:"):
            continue
        gen_model = other
        key = (gen_model, j, row["criterion"])
        if row["winner_source"] == baseline:
            counts[key][0] += 1
        elif row["winner_source"] == "tie":
            counts[key][1] += 1
        else:
            counts[key][2] += 1
    return {k: ((w + 0.5 * t) / (w + t + l), w + t + l, w, t, l) for k, (w, t, l) in counts.items()}


def matrix(
    judgments_log: pathlib.Path,
    criterion: str | None = None,
    include_contaminated: bool = False,
    current_prefix_hashes: dict[str, str] | None = None,
    include_stale: bool = True,
) -> dict:
    """Compute %-picks-human per (gen_model, judge_model, condition).

    Only counts pairs where one side is human. Ties count as 0.5 for human.

    ``include_contaminated``: by default, rows tagged with a
    ``contamination_note`` field are excluded from the aggregate
    (produced before a blind-judge leak fix; the verdict doesn't
    measure what the matrix claims to measure). Pass True to include
    them and see the pre-fix distribution.

    Returns ``{key: (pct, n, wins, ties, losses)}`` where ``wins`` counts the
    judge picking human, ``losses`` counts it picking the generator, and
    ``ties`` counts explicit ties. ``pct`` treats ties as 0.5.
    """
    # counts[(gen_model, judge_model, condition, criterion)] = [wins, ties, losses]
    counts: dict[tuple[str, str, str, str], list[int]] = collections.defaultdict(lambda: [0, 0, 0])
    for row in jsonl.read_dedup(judgments_log):
        if row.get("verdict") is None:
            continue
        if not include_contaminated and row.get("contamination_note"):
            continue
        if not include_stale and _is_stale_row(row, current_prefix_hashes):
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
        if row["winner_source"] == HUMAN:
            counts[key][0] += 1
        elif row["winner_source"] == "tie":
            counts[key][1] += 1
        else:
            counts[key][2] += 1
    return {k: ((w + 0.5 * t) / (w + t + l), w + t + l, w, t, l) for k, (w, t, l) in counts.items()}


def matrix_by_source(
    judgments_log: pathlib.Path,
    include_contaminated: bool = False,
    current_prefix_hashes: dict[str, str] | None = None,
    include_stale: bool = True,
) -> dict[str, dict]:
    """Same as ``matrix()``, binned by essay source (essay_id prefix before ``__``).

    Returns ``{source_id: matrix_dict}`` where each ``matrix_dict`` has the same
    shape as :func:`matrix`. Single pass over the judgments log.
    """
    counts: dict[str, dict[tuple[str, str, str, str], list[int]]] = collections.defaultdict(
        lambda: collections.defaultdict(lambda: [0, 0, 0])
    )
    for row in jsonl.read_dedup(judgments_log):
        if row.get("verdict") is None:
            continue
        if not include_contaminated and row.get("contamination_note"):
            continue
        if not include_stale and _is_stale_row(row, current_prefix_hashes):
            continue
        a, b = row["source_a"], row["source_b"]
        if a != HUMAN and b != HUMAN:
            continue
        eid = str(row.get("essay_id", ""))
        if "__" not in eid:
            continue
        source_id = eid.split("__", 1)[0]
        other = b if a == HUMAN else a
        cond, gen_model = _strip_prefix(other)
        judge_model = row["judge_model"]
        crit = row["criterion"]
        key = (gen_model, judge_model, cond, crit)
        if row["winner_source"] == HUMAN:
            counts[source_id][key][0] += 1
        elif row["winner_source"] == "tie":
            counts[source_id][key][1] += 1
        else:
            counts[source_id][key][2] += 1
    return {
        source_id: {
            k: ((w + 0.5 * t) / (w + t + l), w + t + l, w, t, l)
            for k, (w, t, l) in src_counts.items()
        }
        for source_id, src_counts in counts.items()
    }


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
            for (gg, jj, cc, crit), row in data.items():
                if gg == g and jj == j and cc == condition:
                    if criterion and crit != criterion:
                        continue
                    pct, n = row[0], row[1]
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
