"""Versus router mounted on the rumil FastAPI app.

Reads versus's JSONL stores + cached essay JSON. Aggregation / pair-shaping
logic lives in ``versus.view`` and ``versus.analyze``; this layer just
wraps those results in typed pydantic envelopes for the frontend.

Config resolution: VERSUS_CONFIG_PATH env var, defaulting to
<repo-root>/versus/config.yaml. The essays-only endpoint works without
config; everything else returns 503 if config is missing.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import os
import pathlib
from collections import defaultdict

import pydantic
from fastapi import APIRouter, HTTPException

from versus import analyze as versus_analyze
from versus import config as versus_config
from versus import diagnostics as versus_diagnostics
from versus import essay as versus_essay
from versus import jsonl as versus_jsonl
from versus import judge as versus_judge
from versus import paraphrase as versus_paraphrase
from versus import prepare as versus_prepare
from versus import view as versus_view

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_DEFAULT_CONFIG = _REPO_ROOT / "versus" / "config.yaml"
_DEFAULT_DATA = _REPO_ROOT / "versus" / "data"


def _config_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("VERSUS_CONFIG_PATH", _DEFAULT_CONFIG))


def _data_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("VERSUS_DATA_DIR", _DEFAULT_DATA))


@functools.lru_cache(maxsize=1)
def _cfg_cached() -> versus_config.Config | None:
    p = _config_path()
    if not p.exists():
        return None
    return versus_config.load(p)


def _cfg_required() -> versus_config.Config:
    cfg = _cfg_cached()
    if cfg is None:
        raise HTTPException(503, f"versus config not found at {_config_path()}")
    return cfg


def _resolve_path(p: pathlib.Path) -> pathlib.Path:
    """JSONL/essay paths in config.yaml are relative to versus/. Anchor them."""
    if p.is_absolute():
        return p
    return _REPO_ROOT / "versus" / p


def _essays_dir() -> pathlib.Path:
    cfg = _cfg_cached()
    if cfg:
        return _resolve_path(cfg.essays.cache_dir)
    return _data_dir() / "essays"


def _iter_essay_paths() -> list[pathlib.Path]:
    """Essay JSONs only — skips ``<id>.verdict.json`` and other companions."""
    d = _essays_dir()
    if not d.exists():
        return []
    return sorted(p for p in d.glob("*.json") if not p.name.endswith(".verdict.json"))


def _load_verdict(essay_id: str) -> dict | None:
    p = _essays_dir() / f"{essay_id}.verdict.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def _build_essays_status(
    cfg: versus_config.Config,
) -> tuple[list[EssayStatus], dict[str, str]]:
    """Compute the per-essay status panel and the {essay_id -> current
    prefix_config_hash} map used to flag stale judgment/completion rows.

    Reads cached essay JSONs + their adjacent ``.verdict.json`` files. If
    no essays are cached, returns empty containers (matrix filtering is a
    no-op when ``current_prefix_hashes`` is empty).
    """
    statuses: list[EssayStatus] = []
    current: dict[str, str] = {}
    exclude = set(cfg.essays.exclude_ids)
    for path in _iter_essay_paths():
        with open(path) as f:
            d = json.load(f)
        if "source_id" not in d:
            continue
        if d["id"] in exclude:
            continue
        essay = versus_essay.Essay(
            id=d["id"],
            source_id=d["source_id"],
            url=d["url"],
            title=d["title"],
            author=d["author"],
            pub_date=d["pub_date"],
            blocks=[versus_essay.Block(**b) for b in d["blocks"]],
            markdown=d.get("markdown", ""),
            image_count=d.get("image_count", 0),
            schema_version=d.get("schema_version", 0),
        )
        task = versus_prepare.prepare(
            essay,
            n_paragraphs=cfg.prefix.n_paragraphs,
            include_headers=cfg.prefix.include_headers,
            length_tolerance=cfg.completion.length_tolerance,
        )
        current[essay.id] = task.prefix_config_hash
        verdict = _load_verdict(essay.id)
        statuses.append(
            EssayStatus(
                essay_id=essay.id,
                title=essay.title,
                schema_version=essay.schema_version,
                current_prefix_hash=task.prefix_config_hash,
                validator_clean=verdict["clean"] if verdict else None,
                validator_issues=len(verdict["issues"]) if verdict else 0,
                validator_model=verdict.get("model") if verdict else None,
            )
        )
    return statuses, current


class EssayMeta(pydantic.BaseModel):
    """Headline metadata for one cached essay."""

    id: str
    title: str
    author: str
    pub_date: str
    url: str


class EssayDetail(pydantic.BaseModel):
    """Essay + the prompts shown to completion / judge / paraphrase models."""

    id: str
    title: str
    author: str
    pub_date: str
    url: str
    markdown: str
    prefix_config_hash: str
    target_words: int
    completion_prompt: str
    judge_prompt_template: str
    paraphrase_prompt_template: str
    criteria: list[str]


class Source(pydantic.BaseModel):
    """One generated continuation (or the held-out human remainder)."""

    source_id: str
    kind: str
    text: str
    words: int
    target: int


class Judgment(pydantic.BaseModel):
    """One pairwise judgment row, shaped for the inspect view."""

    judge_model: str
    judge_model_base: str
    prompt_hash: str | None
    judge_version: str | None
    sampling: dict | None
    criterion: str
    source_a: str
    source_b: str
    display_first: str
    display_second: str
    verdict: str | None
    winner_source: str | None
    preference_label: str | None
    reasoning_preview: str
    is_rumil: bool
    rumil_trace_url: str | None
    rumil_question_id: str | None
    rumil_call_id: str | None
    rumil_run_id: str | None
    rumil_cost_usd: float | None
    contamination_note: str | None


class JudgmentDetail(pydantic.BaseModel):
    """Full judgment row for the side-panel inspector on /versus/results.

    Includes the verbatim prompt + reasoning text + raw provider response,
    so a reader can audit what the judge actually saw and said. Most fields
    are optional because the shape varies across judge variants (OpenRouter
    vs anthropic vs rumil:text vs rumil:ws/orch vs human:*).
    """

    key: str
    essay_id: str
    prefix_config_hash: str
    source_a: str
    source_b: str
    display_first: str
    display_second: str
    criterion: str
    judge_model: str
    judge_model_base: str
    prompt_hash: str | None
    judge_version: str | None
    sampling: dict | None
    verdict: str | None
    winner_source: str | None
    preference_label: str | None
    is_rumil: bool
    contamination_note: str | None

    prompt: str | None
    reasoning_text: str | None
    raw_response: dict | list | None

    rumil_trace_url: str | None
    rumil_question_id: str | None
    rumil_call_id: str | None
    rumil_run_id: str | None
    rumil_cost_usd: float | None

    ts: str | None
    duration_s: float | None


class JudgeLabel(pydantic.BaseModel):
    """Stacked column-header parts for a judge model id."""

    variant: str | None
    model: str
    task: str | None
    phash: str | None


class Cell(pydantic.BaseModel):
    """One aggregated (gen, judge, condition[, criterion]) cell.

    ``pct`` treats ties as 0.5. ``wins`` / ``ties`` / ``losses`` are the raw
    counts that feed it (``n = wins + ties + losses``). ``tie_frac`` is the
    share of judgments that were explicit ties — so a 50% cell from all-ties
    visually contrasts with a 50/50 split. ``ci_lo`` / ``ci_hi`` is the
    Wilson 95% interval on ``pct`` (None when ``n == 0``).
    """

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


class GenJudgeCell(pydantic.BaseModel):
    """One (gen_model, judge_model) cell with its rendered colors."""

    gen_model: str
    judge_model: str
    cell: Cell


class ConditionMeta(pydantic.BaseModel):
    title: str
    pair: str
    cell_meaning: str
    value_picks: str


class Matrix(pydantic.BaseModel):
    condition: str
    meta: ConditionMeta
    cells: list[GenJudgeCell]


class CriterionMatrix(pydantic.BaseModel):
    criterion: str
    cells: list[GenJudgeCell]


class SmallGridRow(pydantic.BaseModel):
    condition: str
    per_crit: list[CriterionMatrix]


class SourceSummary(pydantic.BaseModel):
    source_id: str
    n: int
    avg_words: int
    avg_delta_pct: float


class JudgmentRow(pydantic.BaseModel):
    """One row in the raw-judgments explorer at the bottom of /results."""

    key: str
    essay_id: str
    source_a: str
    source_b: str
    criterion: str
    judge_model: str
    verdict: str
    winner: str
    preference_label: str | None
    ts: str
    is_rumil: bool
    contamination_note: str | None
    stale: bool


class EssayStatus(pydantic.BaseModel):
    """Per-essay state for the /results "essays" panel.

    Surfaces both validator output (clean? how many issues?) and the
    current ``prefix_config_hash`` so a reader can spot when essay text
    has drifted out from under cached judgments.
    """

    essay_id: str
    title: str
    schema_version: int
    current_prefix_hash: str
    validator_clean: bool | None
    validator_issues: int
    validator_model: str | None


class RowFilter(pydantic.BaseModel):
    """Echoed back on ResultsBundle when the row list is filtered.

    Populated from ``?filter_gen=`` / ``?filter_judge=`` /
    ``?filter_condition=`` / ``?filter_criterion=`` — the cell-drill-in
    linkage from MatrixTable cells into the raw-judgments table below.
    """

    gen: str | None
    judge: str | None
    condition: str | None
    criterion: str | None


class ResultsBundle(pydantic.BaseModel):
    conditions: list[str]
    criteria: list[str]
    active_criterion: str | None
    gen_models: list[str]
    judge_models: list[str]
    judge_labels: dict[str, JudgeLabel]
    main_matrices: list[Matrix]
    small_grid: list[SmallGridRow]
    rows: list[JudgmentRow]
    total_judgments: int
    total_completions: int
    sources_summary: list[SourceSummary]
    essays_status: list[EssayStatus]
    stale_count: int
    current_count: int
    include_stale: bool
    include_contaminated: bool
    row_filter: RowFilter
    rows_total_before_filter: int


class NextPair(pydantic.BaseModel):
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
    criterion: str
    criterion_desc: str
    done_count: int
    total: int


class CriterionStats(pydantic.BaseModel):
    criterion: str
    done: int
    total: int


class JudgingProgress(pydantic.BaseModel):
    """Returned when there is no next pair; tells the UI to show 'done'."""

    name: str
    criterion: str
    criteria: list[str]
    per_criterion: list[CriterionStats]


class NextPairResponse(pydantic.BaseModel):
    """Either a next pair to judge, or progress when nothing's left."""

    pair: NextPair | None
    progress: JudgingProgress


class JudgmentSubmit(pydantic.BaseModel):
    name: str
    criterion: str
    essay_id: str
    prefix_hash: str
    a: str
    b: str
    first_source: str
    second_source: str
    verdict: str  # "A" | "B" | "tie"
    note: str = ""
    force: bool = False


class JudgmentSubmitResult(pydantic.BaseModel):
    key: str
    winner_source: str
    duplicate: bool = False


router = APIRouter(prefix="/api/versus", tags=["versus"])


def _human_judge_id(name: str) -> str:
    return f"human:{name.strip().lower()}"


def _load_essay(essay_id: str) -> versus_essay.Essay | None:
    p = _essays_dir() / f"{essay_id}.json"
    if not p.exists():
        return None
    with open(p) as f:
        d = json.load(f)
    if "source_id" not in d:
        return None
    return versus_essay.Essay(
        id=d["id"],
        source_id=d["source_id"],
        url=d["url"],
        title=d["title"],
        author=d["author"],
        pub_date=d["pub_date"],
        blocks=[versus_essay.Block(**b) for b in d["blocks"]],
        markdown=d.get("markdown", ""),
        schema_version=d.get("schema_version", 0),
    )


@router.get("/essays", response_model=list[EssayMeta])
def list_essays() -> list[EssayMeta]:
    paths = _iter_essay_paths()
    if not paths and not _essays_dir().exists():
        raise HTTPException(503, f"versus essays dir not found: {_essays_dir()}")
    cfg = _cfg_cached()
    exclude = set(cfg.essays.exclude_ids) if cfg else set()
    out: list[EssayMeta] = []
    for p in paths:
        with open(p) as f:
            d = json.load(f)
        if d.get("id") in exclude:
            continue
        out.append(
            EssayMeta(
                id=d["id"],
                title=d["title"],
                author=d.get("author", ""),
                pub_date=d.get("pub_date", ""),
                url=d.get("url", ""),
            )
        )
    return out


@router.get("/essays/{essay_id}", response_model=EssayDetail)
def get_essay(essay_id: str) -> EssayDetail:
    cfg = _cfg_required()
    essay = _load_essay(essay_id)
    if not essay:
        raise HTTPException(404, f"essay {essay_id} not found")
    task = versus_prepare.prepare(
        essay,
        n_paragraphs=cfg.prefix.n_paragraphs,
        include_headers=cfg.prefix.include_headers,
        length_tolerance=cfg.completion.length_tolerance,
    )
    completion_prompt = versus_prepare.render_prompt(
        task,
        include_headers=cfg.prefix.include_headers,
        tolerance=cfg.completion.length_tolerance,
    )
    judge_system, judge_user = versus_judge.render_judge_prompt(
        prefix_text="{{ PREFIX SHOWN TO JUDGE }}",
        dimension=cfg.judging.criteria[0],
        source_a_text="{{ CONTINUATION A }}",
        source_b_text="{{ CONTINUATION B }}",
    )
    judge_prompt_template = (
        f"## SYSTEM PROMPT\n\n{judge_system}\n\n---\n\n## USER MESSAGE\n\n{judge_user}"
    )
    paraphrase_prompt_template = versus_paraphrase.PARAPHRASE_INSTRUCTIONS.replace(
        "{markdown}", "{{ FULL ESSAY MARKDOWN }}"
    )
    return EssayDetail(
        id=essay.id,
        title=essay.title,
        author=essay.author,
        pub_date=essay.pub_date,
        url=essay.url,
        markdown=essay.markdown,
        prefix_config_hash=task.prefix_config_hash,
        target_words=task.target_words,
        completion_prompt=completion_prompt,
        judge_prompt_template=judge_prompt_template,
        paraphrase_prompt_template=paraphrase_prompt_template,
        criteria=list(cfg.judging.criteria),
    )


@router.get("/essays/{essay_id}/sources", response_model=list[Source])
def get_essay_sources(essay_id: str) -> list[Source]:
    cfg = _cfg_required()
    essay = _load_essay(essay_id)
    if not essay:
        raise HTTPException(404, f"essay {essay_id} not found")
    task = versus_prepare.prepare(
        essay,
        n_paragraphs=cfg.prefix.n_paragraphs,
        include_headers=cfg.prefix.include_headers,
        length_tolerance=cfg.completion.length_tolerance,
    )
    # Multiple completion rows can share the same source_id (different
    # sampling_hash); collapse to one per source_id, last-row-wins, to match
    # versus_judge.load_sources_by_essay.
    by_source: dict[str, Source] = {}
    for row in versus_jsonl.read(_resolve_path(cfg.storage.completions_log)):
        if row["essay_id"] != essay.id or row["prefix_config_hash"] != task.prefix_config_hash:
            continue
        by_source[row["source_id"]] = Source(
            source_id=row["source_id"],
            kind=row.get("source_kind", "?"),
            text=row.get("response_text") or "",
            words=row.get("response_words") or 0,
            target=row.get("target_words") or 0,
        )
    return sorted(by_source.values(), key=lambda s: (s.source_id != "human", s.source_id))


@router.get("/essays/{essay_id}/judgments", response_model=list[Judgment])
def get_essay_judgments(essay_id: str) -> list[Judgment]:
    cfg = _cfg_required()
    essay = _load_essay(essay_id)
    if not essay:
        raise HTTPException(404, f"essay {essay_id} not found")
    task = versus_prepare.prepare(
        essay,
        n_paragraphs=cfg.prefix.n_paragraphs,
        include_headers=cfg.prefix.include_headers,
        length_tolerance=cfg.completion.length_tolerance,
    )
    judgments: list[Judgment] = []
    for row in versus_jsonl.read(_resolve_path(cfg.storage.judgments_log)):
        if row.get("essay_id") != essay.id:
            continue
        if row.get("prefix_config_hash") != task.prefix_config_hash:
            continue
        jm = str(row.get("judge_model", ""))
        base, phash, version = versus_judge.parse_judge_model_suffix(jm)
        judgments.append(
            Judgment(
                judge_model=jm,
                judge_model_base=base,
                prompt_hash=phash,
                judge_version=version,
                sampling=row.get("sampling"),
                criterion=row.get("criterion", ""),
                source_a=row.get("source_a", ""),
                source_b=row.get("source_b", ""),
                display_first=row.get("display_first", ""),
                display_second=row.get("display_second", ""),
                verdict=row.get("verdict"),
                winner_source=row.get("winner_source"),
                preference_label=(row.get("preference_label") or row.get("rumil_preference_label")),
                reasoning_preview=(row.get("reasoning_text") or "")[:400],
                is_rumil=jm.startswith("rumil:"),
                rumil_trace_url=row.get("rumil_trace_url"),
                rumil_question_id=row.get("rumil_question_id"),
                rumil_call_id=row.get("rumil_call_id"),
                rumil_run_id=row.get("rumil_run_id"),
                rumil_cost_usd=row.get("rumil_cost_usd"),
                contamination_note=row.get("contamination_note"),
            )
        )
    judgments.sort(key=lambda j: (j.judge_model, j.criterion, j.source_a, j.source_b))
    return judgments


def _cond_meta(cond: str) -> ConditionMeta:
    m = versus_view.COND_META[cond]
    return ConditionMeta(
        title=m.title,
        pair=m.pair,
        cell_meaning=m.cell_meaning,
        value_picks=m.value_picks,
    )


def _cells_out(view_cells: list[versus_view.GenJudgeCell]) -> list[GenJudgeCell]:
    return [
        GenJudgeCell(
            gen_model=vc.gen_model,
            judge_model=vc.judge_model,
            cell=Cell(
                pct=vc.cell.pct,
                n=vc.cell.n,
                wins=vc.cell.wins,
                ties=vc.cell.ties,
                losses=vc.cell.losses,
                tie_frac=vc.cell.tie_frac,
                ci_lo=vc.cell.ci_lo,
                ci_hi=vc.cell.ci_hi,
                bg=vc.cell.bg,
                fg=vc.cell.fg,
            ),
        )
        for vc in view_cells
    ]


def _row_matches_filters(
    row: dict,
    *,
    filter_gen: str | None,
    filter_judge: str | None,
    filter_condition: str | None,
    filter_criterion: str | None,
) -> bool:
    """Apply the matrix-cell filter to a raw judgments row.

    Mirrors how `analyze.matrix` / `analyze.content_test_matrix` classify a
    row: for the "completion" / "paraphrase" conditions the pair is
    ``(human, G)`` or ``(human, paraphrase:G)``; for "content-test" it's
    ``(paraphrase:J, G)`` with the same ``J`` as ``judge_model``.
    """
    if filter_criterion and row.get("criterion") != filter_criterion:
        return False
    jm = str(row.get("judge_model", ""))
    if filter_judge and jm != filter_judge:
        return False
    a = str(row.get("source_a", ""))
    b = str(row.get("source_b", ""))
    if filter_condition == "content-test":
        j_base = versus_judge.base_judge_model(jm)
        baseline = f"paraphrase:{j_base}"
        if baseline not in (a, b):
            return False
        other = b if a == baseline else a
        if other == "human" or other.startswith("paraphrase:"):
            return False
        gen = other
    elif filter_condition in ("completion", "paraphrase"):
        if a != "human" and b != "human":
            return False
        other = b if a == "human" else a
        cond, gen = versus_analyze._strip_prefix(other)
        if cond != filter_condition:
            return False
    elif filter_condition:
        return False
    else:
        gen = None
    if filter_gen:
        if gen is None:
            # No condition pin: accept as long as gen appears anywhere in the
            # pair (either side, and stripping the paraphrase: prefix so the
            # filter matches across conditions).
            _, gen_a = versus_analyze._strip_prefix(a)
            _, gen_b = versus_analyze._strip_prefix(b)
            if filter_gen not in (gen_a, gen_b):
                return False
        elif gen != filter_gen:
            return False
    return True


@router.get("/results", response_model=ResultsBundle)
def get_results(
    criterion: str | None = None,
    include_contaminated: bool = False,
    include_stale: bool = True,
    filter_gen: str | None = None,
    filter_judge: str | None = None,
    filter_condition: str | None = None,
    filter_criterion: str | None = None,
) -> ResultsBundle:
    cfg = _cfg_required()
    judgments_log = _resolve_path(cfg.storage.judgments_log)
    completions_log = _resolve_path(cfg.storage.completions_log)

    essays_status, current_prefix_hashes = _build_essays_status(cfg)

    data = versus_analyze.matrix(
        judgments_log,
        include_contaminated=include_contaminated,
        current_prefix_hashes=current_prefix_hashes,
        include_stale=include_stale,
    )
    content_data = versus_analyze.content_test_matrix(
        judgments_log,
        include_contaminated=include_contaminated,
        current_prefix_hashes=current_prefix_hashes,
        include_stale=include_stale,
    )

    conditions_present = sorted({k[2] for k in data}) if data else []
    conditions = [c for c in ("completion", "paraphrase") if c in conditions_present] or (
        conditions_present
    )
    criteria = cfg.judging.criteria

    present_gens = {k[0] for k in data if k[2] in conditions} | {k[0] for k in content_data}
    present_judges = {k[1] for k in data if k[2] in conditions} | {k[1] for k in content_data}
    gen_models = sorted(present_gens, key=versus_analyze.model_sort_key)
    judge_models = sorted(present_judges, key=versus_analyze.model_sort_key)

    main_matrices: list[Matrix] = []
    for cond in conditions:
        main_matrices.append(
            Matrix(
                condition=cond,
                meta=_cond_meta(cond),
                cells=_cells_out(
                    versus_view.matrix_cells(
                        data, gen_models, judge_models, cond, criterion, keyed_by_condition=True
                    )
                ),
            )
        )
    main_matrices.append(
        Matrix(
            condition="content-test",
            meta=_cond_meta("content-test"),
            cells=_cells_out(
                versus_view.matrix_cells(
                    content_data,
                    gen_models,
                    judge_models,
                    "content-test",
                    criterion,
                    keyed_by_condition=False,
                )
            ),
        )
    )

    small_grid: list[SmallGridRow] = []
    for cond in conditions:
        small_grid.append(
            SmallGridRow(
                condition=cond,
                per_crit=[
                    CriterionMatrix(
                        criterion=crit,
                        cells=_cells_out(
                            versus_view.matrix_cells(
                                data, gen_models, judge_models, cond, crit, keyed_by_condition=True
                            )
                        ),
                    )
                    for crit in criteria
                ],
            )
        )
    small_grid.append(
        SmallGridRow(
            condition="content-test",
            per_crit=[
                CriterionMatrix(
                    criterion=crit,
                    cells=_cells_out(
                        versus_view.matrix_cells(
                            content_data,
                            gen_models,
                            judge_models,
                            "content-test",
                            crit,
                            keyed_by_condition=False,
                        )
                    ),
                )
                for crit in criteria
            ],
        )
    )

    # `total_judgments` counts raw rows (the on-disk count, including any
    # duplicates), but `rows` is dedup-by-key so the rendered table and React
    # keys behave -- matches matrix() / content_test_matrix() above.
    rows: list[JudgmentRow] = []
    total_judgments = sum(1 for _ in versus_jsonl.read(judgments_log))
    rows_total_before_filter = 0
    stale_count = 0
    current_count = 0
    any_filter = bool(filter_gen or filter_judge or filter_condition or filter_criterion)
    for row in versus_jsonl.read_dedup(judgments_log):
        if row.get("verdict") is None:
            continue
        if not include_contaminated and row.get("contamination_note"):
            continue
        is_stale = versus_analyze._is_stale_row(row, current_prefix_hashes)
        if is_stale:
            stale_count += 1
        else:
            current_count += 1
        if not include_stale and is_stale:
            continue
        rows_total_before_filter += 1
        if any_filter and not _row_matches_filters(
            row,
            filter_gen=filter_gen,
            filter_judge=filter_judge,
            filter_condition=filter_condition,
            filter_criterion=filter_criterion,
        ):
            continue
        rows.append(
            JudgmentRow(
                key=row.get("key", ""),
                essay_id=row["essay_id"],
                source_a=row["source_a"],
                source_b=row["source_b"],
                criterion=row["criterion"],
                judge_model=row["judge_model"],
                verdict=row["verdict"],
                winner=row.get("winner_source") or "-",
                preference_label=(row.get("preference_label") or row.get("rumil_preference_label")),
                ts=row["ts"][:16],
                is_rumil=str(row.get("judge_model", "")).startswith("rumil:"),
                contamination_note=row.get("contamination_note"),
                stale=is_stale,
            )
        )

    total_completions = 0
    source_stats: dict[str, dict] = {}
    for row in versus_jsonl.read(completions_log):
        total_completions += 1
        if not include_stale and versus_analyze._is_stale_row(row, current_prefix_hashes):
            continue
        sid = row["source_id"]
        stats = source_stats.setdefault(sid, {"n": 0, "words": 0, "delta": 0.0})
        stats["n"] += 1
        stats["words"] += row.get("response_words") or 0
        if row.get("target_words"):
            stats["delta"] += (row["response_words"] - row["target_words"]) / row["target_words"]
    sources_summary = [
        SourceSummary(
            source_id=sid,
            n=s["n"],
            avg_words=(s["words"] // s["n"]) if s["n"] else 0,
            avg_delta_pct=(s["delta"] / s["n"] * 100) if s["n"] else 0.0,
        )
        for sid, s in sorted(source_stats.items(), key=lambda x: (x[0] != "human", x[0]))
    ]

    return ResultsBundle(
        conditions=conditions,
        criteria=criteria,
        active_criterion=criterion,
        gen_models=gen_models,
        judge_models=judge_models,
        judge_labels={j: JudgeLabel(**versus_analyze.judge_label(j)) for j in judge_models},
        main_matrices=main_matrices,
        small_grid=small_grid,
        rows=rows,
        total_judgments=total_judgments,
        total_completions=total_completions,
        sources_summary=sources_summary,
        essays_status=essays_status,
        stale_count=stale_count,
        current_count=current_count,
        include_stale=include_stale,
        include_contaminated=include_contaminated,
        row_filter=RowFilter(
            gen=filter_gen,
            judge=filter_judge,
            condition=filter_condition,
            criterion=filter_criterion,
        ),
        rows_total_before_filter=rows_total_before_filter,
    )


def _enumerate_pairs(cfg: versus_config.Config):
    return versus_view.enumerate_pairs(
        cfg,
        _resolve_path(cfg.storage.completions_log),
        _iter_essay_paths(),
    )


def _judging_progress(cfg: versus_config.Config, name: str, criterion: str) -> JudgingProgress:
    judge_model = _human_judge_id(name)
    counts: dict[str, int] = defaultdict(int)
    for row in versus_jsonl.read(_resolve_path(cfg.storage.judgments_log)):
        if row.get("judge_model") == judge_model:
            counts[row.get("criterion", "")] += 1
    total = sum(1 for _ in _enumerate_pairs(cfg))
    return JudgingProgress(
        name=name,
        criterion=criterion,
        criteria=list(cfg.judging.criteria),
        per_criterion=[
            CriterionStats(criterion=c, done=counts[c], total=total) for c in cfg.judging.criteria
        ],
    )


@router.get("/next-pair", response_model=NextPairResponse)
def get_next_pair(name: str, criterion: str | None = None) -> NextPairResponse:
    cfg = _cfg_required()
    judge_model = _human_judge_id(name)
    active_criterion = criterion or cfg.judging.criteria[0]

    done: set[str] = set()
    for row in versus_jsonl.read(_resolve_path(cfg.storage.judgments_log)):
        if row.get("judge_model") == judge_model and row.get("criterion") == active_criterion:
            # Re-derive the row's key with the current scheme so legacy
            # rows (pre-order field) are still recognized as done: their
            # stored ``key`` lacks the ``|ab``/``|ba`` suffix. infer_order
            # recovers the single order they were judged in from
            # display_first; we then rebuild the key under the current
            # scheme so the "done" set is comparable to enumeration keys.
            rorder = versus_judge.infer_order(row)
            done.add(
                versus_judge.judgment_key(
                    row["essay_id"],
                    row["prefix_config_hash"],
                    row["source_a"],
                    row["source_b"],
                    row["criterion"],
                    row["judge_model"],
                    rorder,
                )
            )

    all_pairs = list(_enumerate_pairs(cfg))
    total = len(all_pairs)
    done_count = 0
    next_p: versus_view.PairShape | None = None
    for p in all_pairs:
        order = versus_judge.order_from_display_first(p.a, p.b, p.first_source)
        k = versus_judge.judgment_key(
            p.essay_id, p.prefix_hash, p.a, p.b, active_criterion, judge_model, order
        )
        if k in done:
            done_count += 1
        elif next_p is None:
            next_p = p

    progress = _judging_progress(cfg, name, active_criterion)
    if next_p is None:
        return NextPairResponse(pair=None, progress=progress)

    from rumil.versus_bridge import get_rumil_dimension_body

    pair = NextPair(
        criterion=active_criterion,
        criterion_desc=get_rumil_dimension_body(active_criterion),
        done_count=done_count,
        total=total,
        **versus_view.pair_as_dict(next_p),
    )
    return NextPairResponse(pair=pair, progress=progress)


@router.get("/judgments/by-key", response_model=JudgmentDetail)
def get_judgment_by_key(key: str) -> JudgmentDetail:
    """Look up the verbatim row for a single judgment key.

    Used by the side-panel inspector on /versus/results so a reader can see
    the prompt + reasoning + raw response that produced a verdict. The key
    contains `|` and `:` so callers must pass it as a query param.
    """
    cfg = _cfg_required()
    for row in versus_jsonl.read(_resolve_path(cfg.storage.judgments_log)):
        if row.get("key") != key:
            continue
        jm = str(row.get("judge_model", ""))
        base, phash, version = versus_judge.parse_judge_model_suffix(jm)
        return JudgmentDetail(
            key=row["key"],
            essay_id=row.get("essay_id", ""),
            prefix_config_hash=row.get("prefix_config_hash", ""),
            source_a=row.get("source_a", ""),
            source_b=row.get("source_b", ""),
            display_first=row.get("display_first", ""),
            display_second=row.get("display_second", ""),
            criterion=row.get("criterion", ""),
            judge_model=jm,
            judge_model_base=base,
            prompt_hash=phash,
            judge_version=version,
            sampling=row.get("sampling"),
            verdict=row.get("verdict"),
            winner_source=row.get("winner_source"),
            preference_label=(row.get("preference_label") or row.get("rumil_preference_label")),
            is_rumil=jm.startswith("rumil:"),
            contamination_note=row.get("contamination_note"),
            prompt=row.get("prompt"),
            reasoning_text=row.get("reasoning_text"),
            raw_response=row.get("raw_response"),
            rumil_trace_url=row.get("rumil_trace_url"),
            rumil_question_id=row.get("rumil_question_id"),
            rumil_call_id=row.get("rumil_call_id"),
            rumil_run_id=row.get("rumil_run_id"),
            rumil_cost_usd=row.get("rumil_cost_usd"),
            ts=row.get("ts"),
            duration_s=row.get("duration_s"),
        )
    raise HTTPException(404, f"judgment key not found: {key}")


@router.post("/judgments", response_model=JudgmentSubmitResult)
def submit_judgment(body: JudgmentSubmit) -> JudgmentSubmitResult:
    cfg = _cfg_required()
    judge_model = _human_judge_id(body.name)
    if body.verdict == "A":
        winner_source = body.first_source
    elif body.verdict == "B":
        winner_source = body.second_source
    elif body.verdict == "tie":
        winner_source = "tie"
    else:
        raise HTTPException(400, f"bad verdict: {body.verdict}")

    order = versus_judge.order_from_display_first(body.a, body.b, body.first_source)
    k = versus_judge.judgment_key(
        body.essay_id, body.prefix_hash, body.a, body.b, body.criterion, judge_model, order
    )
    log_path = _resolve_path(cfg.storage.judgments_log)
    # Guard against double-clicks / accidental re-judgment. read_dedup
    # collapses dupes last-row-wins, but the raw file grows and
    # total_judgments inflates until caches roll over. Require an
    # explicit `force` to override -- lets the operator fix a mistaken
    # verdict, but makes "I clicked twice" a no-op.
    if not body.force and k in versus_jsonl.keys(log_path):
        return JudgmentSubmitResult(key=k, winner_source=winner_source, duplicate=True)
    row = {
        "key": k,
        "essay_id": body.essay_id,
        "prefix_config_hash": body.prefix_hash,
        "source_a": body.a,
        "source_b": body.b,
        "display_first": body.first_source,
        "display_second": body.second_source,
        "order": order,
        "criterion": body.criterion,
        "judge_model": judge_model,
        "verdict": body.verdict,
        "winner_source": winner_source,
        "reasoning_text": body.note,
        "prompt": None,
        "ts": dt.datetime.now(dt.UTC).isoformat(),
        "duration_s": None,
        "raw_response": None,
    }
    versus_jsonl.append(log_path, row)
    return JudgmentSubmitResult(key=k, winner_source=winner_source)


class JudgeBiasRowOut(pydantic.BaseModel):
    """Per-judge A-preference breakdown.

    `all_*` covers every judged row; `cvc_*` covers only
    completion-vs-completion pairs (neither side is human), which is the
    pure-position-bias signal. `content_bias_pp` is the all - cvc gap in
    percentage points; null when cvc n<20.
    """

    judge_base: str
    n_total: int
    all_a_pct: float
    all_ci_lo_pct: float
    all_ci_hi_pct: float
    n_cvc: int
    cvc_a_pct: float | None
    cvc_ci_lo_pct: float | None
    cvc_ci_hi_pct: float | None
    content_bias_pp: float | None


class SmallNCellOut(pydantic.BaseModel):
    gen_model: str
    judge_base: str
    condition: str
    criterion: str
    n: int


class EssayFlagOut(pydantic.BaseModel):
    essay_id: str
    title: str
    n_judgments: int
    tie_rate_pct: float
    tie_flag: bool
    sweep_source: str | None
    sweep_n: int


class DiagnosticsBundle(pydantic.BaseModel):
    """Summary counts + three sections for the Diagnostics pane.

    `biased_judge_count` uses a |A%-50| > 5pp threshold so the banner
    line matches the default color thresholds in the UI.
    """

    judge_bias: list[JudgeBiasRowOut]
    biased_judge_count: int
    small_n_cells: list[SmallNCellOut]
    essay_flags: list[EssayFlagOut]


@router.get("/diagnostics", response_model=DiagnosticsBundle)
def get_diagnostics(
    criterion: str | None = None,
    include_contaminated: bool = False,
    include_stale: bool = True,
) -> DiagnosticsBundle:
    """Post-hoc bias / n-floor / per-essay sanity over the judgments log.

    Filters mirror /results so the pane's numbers line up with the
    matrix the operator is currently looking at.
    """
    cfg = _cfg_required()
    judgments_log = _resolve_path(cfg.storage.judgments_log)

    _, current_prefix_hashes = _build_essays_status(cfg)

    titles: dict[str, str] = {}
    for p in _iter_essay_paths():
        with open(p) as f:
            d = json.load(f)
        titles[d["id"]] = d.get("title", d["id"])

    filtered_rows: list[dict] = []
    for row in versus_jsonl.read_dedup(judgments_log):
        if row.get("verdict") is None:
            continue
        if not include_contaminated and row.get("contamination_note"):
            continue
        if not include_stale and versus_analyze._is_stale_row(row, current_prefix_hashes):
            continue
        if criterion is not None and row.get("criterion") != criterion:
            continue
        filtered_rows.append(row)

    bias = versus_diagnostics.judge_bias_rows(filtered_rows)
    small = versus_diagnostics.small_n_cells(filtered_rows)
    flags = versus_diagnostics.essay_flags(filtered_rows)

    bias_out = [
        JudgeBiasRowOut(
            judge_base=r.judge_base,
            n_total=r.n_total,
            all_a_pct=(r.all_a_rate or 0.0) * 100,
            all_ci_lo_pct=(r.all_ci_lo or 0.0) * 100,
            all_ci_hi_pct=(r.all_ci_hi or 0.0) * 100,
            n_cvc=r.n_cvc,
            cvc_a_pct=(r.cvc_a_rate * 100) if r.cvc_a_rate is not None else None,
            cvc_ci_lo_pct=(r.cvc_ci_lo * 100) if r.cvc_ci_lo is not None else None,
            cvc_ci_hi_pct=(r.cvc_ci_hi * 100) if r.cvc_ci_hi is not None else None,
            content_bias_pp=(r.content_bias * 100) if r.content_bias is not None else None,
        )
        for r in bias
    ]
    small_out = [
        SmallNCellOut(
            gen_model=c.gen_model,
            judge_base=c.judge_base,
            condition=c.condition,
            criterion=c.criterion,
            n=c.n,
        )
        for c in small
    ]
    flags_out = [
        EssayFlagOut(
            essay_id=f.essay_id,
            title=titles.get(f.essay_id, f.essay_id),
            n_judgments=f.n_judgments,
            tie_rate_pct=f.tie_rate * 100,
            tie_flag=f.tie_flag,
            sweep_source=f.sweep_source,
            sweep_n=f.sweep_n,
        )
        for f in flags
    ]
    return DiagnosticsBundle(
        judge_bias=bias_out,
        biased_judge_count=versus_diagnostics.biased_judge_count(bias),
        small_n_cells=small_out,
        essay_flags=flags_out,
    )
