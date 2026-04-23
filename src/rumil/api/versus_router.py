"""Versus router mounted on the rumil FastAPI app.

Reads versus's JSONL stores + cached essay JSON. Aggregation logic stays
in versus.analyze; this layer just shapes typed responses.

Config resolution: VERSUS_CONFIG_PATH env var, defaulting to
<repo-root>/versus/config.yaml. The essays-only endpoint works without
config; everything else returns 503 if config is missing.
"""

from __future__ import annotations

import datetime as dt
import functools
import itertools
import json
import os
import pathlib
from collections import defaultdict
from collections.abc import Sequence

import pydantic
from fastapi import APIRouter, HTTPException

from versus import analyze as versus_analyze
from versus import config as versus_config
from versus import fetch as versus_fetch
from versus import jsonl as versus_jsonl
from versus import judge as versus_judge
from versus import paraphrase as versus_paraphrase
from versus import prepare as versus_prepare

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
    criterion: str
    source_a: str
    source_b: str
    display_first: str
    display_second: str
    verdict: str | None
    winner_source: str | None
    reasoning_preview: str
    is_rumil: bool
    rumil_trace_url: str | None
    rumil_preference_label: str | None
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
    verdict: str | None
    winner_source: str | None
    is_rumil: bool
    contamination_note: str | None

    prompt: str | None
    reasoning_text: str | None
    raw_response: dict | list | None

    rumil_trace_url: str | None
    rumil_preference_label: str | None
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
    pct: float | None
    n: int
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
    ts: str
    is_rumil: bool
    contamination_note: str | None


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


class JudgmentSubmitResult(pydantic.BaseModel):
    key: str
    winner_source: str


router = APIRouter(prefix="/api/versus", tags=["versus"])


def _human_judge_id(name: str) -> str:
    return f"human:{name.strip().lower()}"


def _load_essay(essay_id: str) -> versus_fetch.Essay | None:
    p = _essays_dir() / f"{essay_id}.json"
    if not p.exists():
        return None
    with open(p) as f:
        d = json.load(f)
    return versus_fetch.Essay(
        id=d["id"],
        url=d["url"],
        title=d["title"],
        author=d["author"],
        pub_date=d["pub_date"],
        blocks=[versus_fetch.Block(**b) for b in d["blocks"]],
        markdown=d.get("markdown", ""),
        schema_version=d.get("schema_version", 0),
    )


@router.get("/essays", response_model=list[EssayMeta])
def list_essays() -> list[EssayMeta]:
    essays_dir = _essays_dir()
    if not essays_dir.exists():
        raise HTTPException(503, f"versus essays dir not found: {essays_dir}")
    out: list[EssayMeta] = []
    for p in sorted(essays_dir.glob("*.json")):
        with open(p) as f:
            d = json.load(f)
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
    judge_prompt_template = versus_judge.render_judge_prompt(
        prefix_text="{{ PREFIX SHOWN TO JUDGE }}",
        criterion=cfg.judging.criteria[0],
        source_a_text="{{ CONTINUATION A }}",
        source_b_text="{{ CONTINUATION B }}",
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
        judgments.append(
            Judgment(
                judge_model=row.get("judge_model", ""),
                criterion=row.get("criterion", ""),
                source_a=row.get("source_a", ""),
                source_b=row.get("source_b", ""),
                display_first=row.get("display_first", ""),
                display_second=row.get("display_second", ""),
                verdict=row.get("verdict"),
                winner_source=row.get("winner_source"),
                reasoning_preview=(row.get("reasoning_text") or "")[:400],
                is_rumil=str(row.get("judge_model", "")).startswith("rumil:"),
                rumil_trace_url=row.get("rumil_trace_url"),
                rumil_preference_label=row.get("rumil_preference_label"),
                rumil_question_id=row.get("rumil_question_id"),
                rumil_call_id=row.get("rumil_call_id"),
                rumil_run_id=row.get("rumil_run_id"),
                rumil_cost_usd=row.get("rumil_cost_usd"),
                contamination_note=row.get("contamination_note"),
            )
        )
    judgments.sort(key=lambda j: (j.judge_model, j.criterion, j.source_a, j.source_b))
    return judgments


_COND_META = {
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


def _build_cell(
    data: dict,
    gen: str,
    jmod: str,
    cond: str,
    crit_filter: str | None,
    *,
    keyed_by_condition: bool,
) -> Cell:
    hs, ns = 0.0, 0
    for k, (pct, n) in data.items():
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
        hs += pct * n
        ns += n
    pct = (hs / ns) if ns else None
    return Cell(
        pct=pct,
        n=ns,
        bg=versus_analyze.cell_color(pct) if pct is not None else "#f4f4f0",
        fg=versus_analyze.text_color(pct) if pct is not None else "#999",
    )


def _matrix_cells(
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
            cell=_build_cell(data, g, j, cond, crit, keyed_by_condition=keyed_by_condition),
        )
        for g in gen_models
        for j in judge_models
    ]


@router.get("/results", response_model=ResultsBundle)
def get_results(
    criterion: str | None = None,
    include_contaminated: bool = False,
) -> ResultsBundle:
    cfg = _cfg_required()
    judgments_log = _resolve_path(cfg.storage.judgments_log)
    completions_log = _resolve_path(cfg.storage.completions_log)

    data = versus_analyze.matrix(judgments_log, include_contaminated=include_contaminated)
    content_data = versus_analyze.content_test_matrix(
        judgments_log, include_contaminated=include_contaminated
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
                meta=_COND_META[cond],
                cells=_matrix_cells(
                    data, gen_models, judge_models, cond, criterion, keyed_by_condition=True
                ),
            )
        )
    main_matrices.append(
        Matrix(
            condition="content-test",
            meta=_COND_META["content-test"],
            cells=_matrix_cells(
                content_data,
                gen_models,
                judge_models,
                "content-test",
                criterion,
                keyed_by_condition=False,
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
                        cells=_matrix_cells(
                            data, gen_models, judge_models, cond, crit, keyed_by_condition=True
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
                    cells=_matrix_cells(
                        content_data,
                        gen_models,
                        judge_models,
                        "content-test",
                        crit,
                        keyed_by_condition=False,
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
    for row in versus_jsonl.read_dedup(judgments_log):
        if row.get("verdict") is None:
            continue
        if not include_contaminated and row.get("contamination_note"):
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
                ts=row["ts"][:16],
                is_rumil=str(row.get("judge_model", "")).startswith("rumil:"),
                contamination_note=row.get("contamination_note"),
            )
        )

    total_completions = 0
    source_stats: dict[str, dict] = {}
    for row in versus_jsonl.read(completions_log):
        total_completions += 1
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
    )


def _enumerate_pairs(cfg: versus_config.Config):
    """Yield NextPair-shaped dicts (without progress) for every blind pair."""
    groups, prefix_texts = versus_judge.load_sources_by_essay(
        _resolve_path(cfg.storage.completions_log)
    )
    titles: dict[str, str] = {}
    essays_dir = _essays_dir()
    if essays_dir.exists():
        for p in essays_dir.glob("*.json"):
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
            yield {
                "essay_id": essay_id,
                "prefix_hash": prefix_hash,
                "a": a_id,
                "b": b_id,
                "first_source": first.source_id,
                "second_source": second.source_id,
                "first_text": first.text,
                "second_text": second.text,
                "prefix_text": prefix_text,
                "title": titles.get(essay_id, essay_id),
            }


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
            done.add(row["key"])

    all_pairs = list(_enumerate_pairs(cfg))
    total = len(all_pairs)
    done_count = 0
    next_p: dict | None = None
    for p in all_pairs:
        k = versus_judge.judgment_key(
            p["essay_id"], p["prefix_hash"], p["a"], p["b"], active_criterion, judge_model
        )
        if k in done:
            done_count += 1
        elif next_p is None:
            next_p = p

    progress = _judging_progress(cfg, name, active_criterion)
    if next_p is None:
        return NextPairResponse(pair=None, progress=progress)

    pair = NextPair(
        criterion=active_criterion,
        criterion_desc=versus_judge.CRITERION_PROMPTS[active_criterion],
        done_count=done_count,
        total=total,
        **next_p,
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
        return JudgmentDetail(
            key=row["key"],
            essay_id=row.get("essay_id", ""),
            prefix_config_hash=row.get("prefix_config_hash", ""),
            source_a=row.get("source_a", ""),
            source_b=row.get("source_b", ""),
            display_first=row.get("display_first", ""),
            display_second=row.get("display_second", ""),
            criterion=row.get("criterion", ""),
            judge_model=row.get("judge_model", ""),
            verdict=row.get("verdict"),
            winner_source=row.get("winner_source"),
            is_rumil=str(row.get("judge_model", "")).startswith("rumil:"),
            contamination_note=row.get("contamination_note"),
            prompt=row.get("prompt"),
            reasoning_text=row.get("reasoning_text"),
            raw_response=row.get("raw_response"),
            rumil_trace_url=row.get("rumil_trace_url"),
            rumil_preference_label=row.get("rumil_preference_label"),
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

    k = versus_judge.judgment_key(
        body.essay_id, body.prefix_hash, body.a, body.b, body.criterion, judge_model
    )
    row = {
        "key": k,
        "essay_id": body.essay_id,
        "prefix_config_hash": body.prefix_hash,
        "source_a": body.a,
        "source_b": body.b,
        "display_first": body.first_source,
        "display_second": body.second_source,
        "criterion": body.criterion,
        "judge_model": judge_model,
        "verdict": body.verdict,
        "winner_source": winner_source,
        "reasoning_text": body.note,
        "prompt": None,
        "ts": dt.datetime.utcnow().isoformat() + "Z",
        "duration_s": None,
        "raw_response": None,
    }
    versus_jsonl.append(_resolve_path(cfg.storage.judgments_log), row)
    return JudgmentSubmitResult(key=k, winner_source=winner_source)
