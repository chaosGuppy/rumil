"""Versus router mounted on the rumil FastAPI app.

Reads versus's JSONL stores + cached essay JSON. Aggregation /
pair-shaping logic lives in ``versus.view`` and ``versus.analyze``;
this layer just wraps those results in typed pydantic envelopes for
the frontend.

Config resolution: VERSUS_CONFIG_PATH env var, defaulting to
<repo-root>/versus/config.yaml. The essays-only endpoint works
without config; everything else returns 503 if config is missing.
"""

from __future__ import annotations

import functools
import os
import pathlib
import time

import pydantic
from fastapi import APIRouter, Depends, HTTPException

from rumil.api.auth import require_admin
from rumil.settings import get_settings
from versus import analyze as versus_analyze
from versus import config as versus_config
from versus import diagnostics as versus_diagnostics
from versus import essay as versus_essay
from versus import judge as versus_judge
from versus import mainline as versus_mainline
from versus import prepare as versus_prepare
from versus import versus_db
from versus import view as versus_view

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_DEFAULT_CONFIG = _REPO_ROOT / "versus" / "config.yaml"
_DEFAULT_DATA = _REPO_ROOT / "versus" / "data"


def _legacy_judgment_dict(row: dict) -> dict:
    """Translate a versus_judgments DB row into the legacy JSONL shape.

    The router and its downstream consumers (versus.diagnostics, the FE
    schemas) were written against the JSONL field names. Rather than
    rewriting all of that to the new column names, we translate at the
    boundary: the rest of the router can keep saying ``row["raw_response"]``,
    ``row["config"]``, ``row["prefix_config_hash"]``, etc.
    """
    judge_inputs = row.get("judge_inputs") or {}
    request = row.get("request") or None
    sys_prompt: str | None = None
    user_prompt: str | None = None
    if isinstance(request, dict):
        if "system" in request:
            sys_prompt = request.get("system")
            messages = request.get("messages") or []
            if messages:
                user_prompt = (messages[0] or {}).get("content")
        else:
            messages = request.get("messages") or []
            if len(messages) >= 1 and (messages[0] or {}).get("role") == "system":
                sys_prompt = (messages[0] or {}).get("content")
                if len(messages) >= 2:
                    user_prompt = (messages[1] or {}).get("content")
            elif messages:
                user_prompt = (messages[0] or {}).get("content")
    return {
        "key": row["id"],
        "essay_id": row.get("essay_id"),
        "prefix_config_hash": row.get("prefix_hash"),
        "source_a": row.get("source_a"),
        "source_b": row.get("source_b"),
        "display_first": row.get("display_first"),
        "display_second": _other_source(row),
        "criterion": row.get("criterion"),
        "judge_model": row.get("judge_model"),
        "verdict": row.get("verdict"),
        "winner_source": row.get("winner_source"),
        "preference_label": row.get("preference_label"),
        "reasoning_text": row.get("reasoning_text"),
        "prompt": user_prompt,
        "system_prompt": sys_prompt,
        "raw_response": row.get("response"),
        "ts": row.get("created_at"),
        "duration_s": row.get("duration_s"),
        "config": judge_inputs,
        "config_hash": row.get("judge_inputs_hash"),
        "sampling": judge_inputs.get("sampling"),
        "rumil_call_id": row.get("rumil_call_id"),
        "rumil_run_id": row.get("run_id"),
        "rumil_question_id": row.get("rumil_question_id"),
        "rumil_trace_url": _trace_url(row.get("run_id"), row.get("rumil_call_id")),
        "rumil_cost_usd": row.get("rumil_cost_usd"),
        "contamination_note": row.get("contamination_note"),
    }


def _trace_url(run_id: str | None, call_id: str | None) -> str | None:
    """Compose a frontend trace URL from run + call ids; None if no run."""
    if not run_id:
        return None
    base = get_settings().frontend_url.rstrip("/")
    anchor = f"#call-{call_id[:8]}" if call_id else ""
    return f"{base}/traces/{run_id}{anchor}"


def _other_source(row: dict) -> str | None:
    """Resolve display_second from display_first + (source_a, source_b)."""
    df = row.get("display_first")
    sa = row.get("source_a")
    sb = row.get("source_b")
    if df == sa:
        return sb
    if df == sb:
        return sa
    return None


def _legacy_text_dict(row: dict) -> dict:
    """Translate a versus_texts DB row into the legacy completion JSONL shape."""
    params = row.get("params") or {}
    text = row.get("text") or ""
    return {
        "key": row["id"],
        "essay_id": row.get("essay_id"),
        "prefix_config_hash": row.get("prefix_hash"),
        "source_id": row.get("source_id"),
        "source_kind": row.get("kind"),
        "model_id": row.get("model_id"),
        "params": {
            k: v
            for k, v in params.items()
            if k not in {"raw_response_text", "ts", "duration_s", "provider", "target_words"}
        },
        "prompt": _user_prompt_from_request(row.get("request")),
        "response_text": text,
        # Prefer the generated response_words column when present (light
        # projection); fall back to a Python split on the full text for
        # callers that fetched the heavy projection.
        "response_words": row["response_words"] if "response_words" in row else len(text.split()),
        "target_words": params.get("target_words", 0),
        "ts": params.get("ts") or row.get("created_at"),
        "duration_s": params.get("duration_s"),
        "raw_response": row.get("response"),
        "provider": params.get("provider"),
        # sampling_hash is no longer stored — recompute from the params for
        # legacy callers that still display it; falls back to "-".
        "sampling_hash": "-",
    }


def _user_prompt_from_request(request: dict | None) -> str | None:
    if not isinstance(request, dict):
        return None
    messages = request.get("messages") or []
    for m in messages:
        if (m or {}).get("role") == "user":
            return m.get("content")
    if messages:
        return (messages[0] or {}).get("content")
    return None


# Process-local cache for the "light" projections of versus_judgments and
# versus_texts. The aggregator endpoints (/results, /diagnostics) refetch
# every page load; without caching, clicking around the UI hits the DB
# repeatedly. The previous JSONL store had near-instant reads via an
# in-memory cache keyed on (path, mtime, size); we approximate that here
# with a small TTL. Stale-by-up-to-N-seconds is fine for an interactive
# eval-results view — fresh data costs a hard refresh.
_LIGHT_CACHE_TTL_S = 10.0
_LIGHT_CACHE: dict[str, tuple[float, list[dict]]] = {}
# Per-essay cache for the inspect view's per-variant fetches. Server-side
# essay_id filter cuts the payload to ~5-30 rows; the cache makes switching
# back to a recently-viewed essay instant.
_PER_ESSAY_CACHE: dict[tuple[str, str], tuple[float, list[dict]]] = {}


def _per_essay_load(kind: str, essay_id: str) -> list[dict]:
    """Cached fetch of judgments/texts scoped to one essay.

    Returns the full (heavy) projection because the per-essay inspect
    view renders prompts, reasoning_text, and the source-text content.
    """
    key = (kind, essay_id)
    now = time.time()
    cached = _PER_ESSAY_CACHE.get(key)
    if cached is not None and now - cached[0] < _LIGHT_CACHE_TTL_S:
        return cached[1]
    client = versus_db.get_client()
    if kind == "judgments":
        rows = list(versus_db.iter_judgments(client, essay_id=essay_id))
    elif kind == "texts":
        rows = list(versus_db.iter_texts(client, essay_id=essay_id))
    else:
        raise ValueError(f"unknown kind: {kind}")
    _PER_ESSAY_CACHE[key] = (now, rows)
    return rows


def _load_essay_rows() -> list[dict]:
    """Cached fetch of all versus_essays rows.

    Reuses the LIGHT cache slot under key "essays" — the table is small
    (~30 rows) and the row payload includes markdown / blocks which we
    do read per request to compute prefix_hash and render content.
    """
    now = time.time()
    cached = _LIGHT_CACHE.get("essays")
    if cached is not None and now - cached[0] < _LIGHT_CACHE_TTL_S:
        return cached[1]
    client = versus_db.get_client()
    rows = list(versus_db.iter_essays(client))
    _LIGHT_CACHE["essays"] = (now, rows)
    return rows


def _light_load(kind: str) -> list[dict]:
    """Cached fetch of judgments/texts via the light projection."""
    now = time.time()
    cached = _LIGHT_CACHE.get(kind)
    if cached is not None and now - cached[0] < _LIGHT_CACHE_TTL_S:
        return cached[1]
    client = versus_db.get_client()
    if kind == "judgments":
        rows = list(versus_db.iter_judgments(client, light=True))
    elif kind == "texts":
        rows = list(versus_db.iter_texts(client, light=True))
    else:
        raise ValueError(f"unknown kind: {kind}")
    _LIGHT_CACHE[kind] = (now, rows)
    return rows


def _config_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("VERSUS_CONFIG_PATH", _DEFAULT_CONFIG))


def _data_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("VERSUS_DATA_DIR", _DEFAULT_DATA))


@functools.lru_cache(maxsize=1)
def _cfg_load_cached(path: str) -> versus_config.Config:
    return versus_config.load(path)


def _cfg_cached() -> versus_config.Config | None:
    p = _config_path()
    if not p.exists():
        return None
    return _cfg_load_cached(str(p))


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


def _build_completion_source_index_from_rows(
    text_rows: list[dict],
) -> dict[tuple[str, str], set[str]]:
    """Index ``(essay_id, prefix_hash) -> {source_id, ...}`` over pre-loaded texts.

    Used to flag judgment rows as ``orphaned``: a row whose ``prefix_hash``
    IS current but whose ``source_a`` / ``source_b`` has no matching text
    row (e.g. the text was manually removed, or judgment ran against a
    partially-generated set). Distinct from staleness, which fires when
    the prefix_hash itself is old.

    Reads DB-shaped rows (versus_texts.prefix_hash). Callers passing
    legacy-shaped rows should rename ``prefix_hash`` before calling.
    """
    index: dict[tuple[str, str], set[str]] = {}
    for row in text_rows:
        eid = row.get("essay_id")
        ph = row.get("prefix_hash")
        sid = row.get("source_id")
        if not (eid and ph and sid):
            continue
        index.setdefault((eid, ph), set()).add(sid)
    return index


def _is_orphaned(row: dict, sources: dict[tuple[str, str], set[str]]) -> bool:
    key = (row.get("essay_id"), row.get("prefix_config_hash"))
    present = sources.get(key)  # pyright: ignore[reportArgumentType]
    if present is None:
        return False  # unknown (essay, prefix) — treat as stale, not orphaned
    return row.get("source_a") not in present or row.get("source_b") not in present


def _resolve_prefix_label(cfg: versus_config.Config, label: str | None) -> versus_config.PrefixCfg:
    """Look up a prefix variant by id; HTTP 400 on unknown."""
    try:
        return versus_prepare.resolve_prefix_cfg(cfg, label)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _build_essays_status(
    cfg: versus_config.Config,
    *,
    prefix_cfg: versus_config.PrefixCfg | None = None,
) -> tuple[list[EssayStatus], dict[str, str]]:
    """Compute the per-essay status panel and the {essay_id -> current
    prefix_config_hash} map used to flag stale judgment/completion rows.

    ``prefix_cfg`` defaults to ``cfg.prefix``. Pass a sibling from
    ``cfg.prefix_variants`` to compute the map under that variant — this
    is how the UI scopes staleness to a selected variant.

    Reads from versus_essays. If the table is empty, returns empty
    containers (matrix filtering is a no-op when ``current_prefix_hashes``
    is empty).
    """
    pcfg = prefix_cfg if prefix_cfg is not None else cfg.prefix
    statuses: list[EssayStatus] = []
    current: dict[str, str] = {}
    exclude = set(cfg.essays.exclude_ids)
    for row in _load_essay_rows():
        if row["id"] in exclude:
            continue
        if not versus_essay.is_current_schema(row):
            continue
        essay = versus_prepare.essay_from_db_row(row)
        task = versus_prepare.prepare(
            essay,
            n_paragraphs=pcfg.n_paragraphs,
            include_headers=pcfg.include_headers,
            length_tolerance=cfg.completion.length_tolerance,
        )
        current[essay.id] = task.prefix_config_hash
        statuses.append(
            EssayStatus(
                essay_id=essay.id,
                title=essay.title,
                schema_version=essay.schema_version,
                current_prefix_hash=task.prefix_config_hash,
                validator_clean=row.get("verdict_clean"),
                validator_issues=len(row.get("verdict_issues") or []),
                validator_model=row.get("verdict_model"),
            )
        )
    return statuses, current


class EssayMeta(pydantic.BaseModel):
    """Headline metadata for one cached essay."""

    id: str
    source_id: str
    title: str
    author: str
    pub_date: str
    url: str
    schema_version: int


class EssayDetail(pydantic.BaseModel):
    """Essay + the prompts shown to completion / judge models."""

    id: str
    title: str
    author: str
    pub_date: str
    url: str
    markdown: str
    schema_version: int
    prefix_config_hash: str
    target_words: int
    completion_prompt: str
    judge_system_prompt_template: str
    judge_user_prompt_template: str
    # Hash of the rendered judge system prompt for criteria[0], blind
    # path. Matches the ``p<hash>`` suffix in ``judge_model`` strings
    # on judgment rows — lets the UI correlate "this template" with
    # "rows that used it".
    judge_prompt_hash: str
    criteria: list[str]
    # Available prefix variants and the one this response was rendered
    # for. Used by the /versus/inspect dropdown so users can flip the
    # prompt + completions + judgments view between variants.
    prefix_variants: list[PrefixVariantInfo]
    active_prefix_label: str


class Source(pydantic.BaseModel):
    """One generated continuation (or the held-out human remainder).

    ``prompt`` is the verbatim completion prompt the model received,
    stored on the row (legacy rows and the human baseline have no
    prompt — the inspect UI falls back to the template in that case).
    """

    source_id: str
    kind: str
    text: str
    words: int
    target: int
    prompt: str | None
    prefix_config_hash: str
    sampling_hash: str | None
    model_id: str | None


class Judgment(pydantic.BaseModel):
    """One pairwise judgment row, shaped for the inspect view.

    Includes the full ``reasoning_text`` / ``prompt`` / ``system_prompt``
    so the inspect UI can render them inline (matching the standalone
    self-vs-human HTML viewer). Older rows may not have ``system_prompt``
    or ``prompt`` populated; both are optional.
    """

    judge_model: str
    judge_model_id: str  # the underlying model id, e.g. "claude-opus-4-7"
    config_hash: str
    prompt_hash: str
    sampling: dict
    criterion: str
    source_a: str
    source_b: str
    display_first: str
    display_second: str
    verdict: str | None
    winner_source: str | None
    preference_label: str | None
    reasoning_preview: str
    reasoning_text: str | None
    prompt: str | None
    system_prompt: str | None
    is_rumil: bool
    rumil_trace_url: str | None
    rumil_question_id: str | None
    rumil_call_id: str | None
    rumil_run_id: str | None
    rumil_cost_usd: float | None
    contamination_note: str | None
    orphaned: bool
    prefix_config_hash: str


class JudgmentDetail(pydantic.BaseModel):
    """Full judgment row for the side-panel inspector on /versus/results.

    Includes the verbatim prompt + reasoning text + raw provider response,
    so a reader can audit what the judge actually saw and said. Most fields
    are optional because the shape varies across judge variants (OpenRouter
    vs anthropic vs rumil:text vs rumil:ws/orch).
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
    judge_model_id: str
    config_hash: str
    prompt_hash: str
    sampling: dict
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


class SourceMatrix(pydantic.BaseModel):
    """A main matrix restricted to one essay source (e.g. forethought / redwood / carlsmith).

    Wraps a regular :class:`Matrix` so the gen/judge axes match the all-source
    matrix above; ``source_id`` is the essay-source label rendered in the UI.
    """

    source_id: str
    matrix: Matrix


class SourceSummary(pydantic.BaseModel):
    source_id: str
    n: int
    avg_words: int
    avg_delta_pct: float


class JudgmentRow(pydantic.BaseModel):
    """One row in the raw-judgments explorer at the bottom of /results.

    ``source_a``/``source_b`` are the alphabetical canonical ordering used
    for the dedup key — stable identity across rows. ``display_first``/
    ``display_second`` are what the judge actually saw as "Continuation A"
    and "B"; they diverge from canonical roughly half the time. The
    ``verdict`` letter (``A``/``B``/``tie``) refers to display order, not
    canonical, so the rows-table renders display order alongside the
    canonical pair to keep the mapping legible.
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
    judge_model_id: str
    config_hash: str
    verdict: str
    winner: str
    preference_label: str | None
    ts: str
    is_rumil: bool
    contamination_note: str | None
    stale: bool
    orphaned: bool


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


class PrefixVariantInfo(pydantic.BaseModel):
    """One prefix variant available in the active config.

    Surfaced on ResultsBundle so the /versus/results UI can render a
    variant selector. ``id`` matches the yaml ``prefix.id`` /
    ``prefix_variants[].id`` and is the value the UI passes back as
    ``?prefix_label=<id>``.
    """

    id: str
    n_paragraphs: int
    include_headers: bool


class ProvenanceAxis(pydantic.BaseModel):
    """One axis of the provenance summary.

    ``description`` says what the value or hash is computed over, so
    the operator knows what would change it. ``counts`` is value ->
    row count over the surviving rows. ``current_values`` is the
    mainline set (UI flags anything not in this list as non-current).
    ``value_labels`` turns opaque hashes into readable strings where
    derivable (e.g. a ``prefix_config_hash`` maps back to its
    originating ``"essay_id / variant_id"``).
    """

    description: str
    counts: dict[str, int]
    current_values: list[str]
    value_labels: dict[str, str]


class ProvenanceSummary(pydantic.BaseModel):
    """Per-axis breakdown of what slice the aggregate sits on top of.

    ``axes`` is keyed by axis name; ``axis_order`` declares the panel
    rendering order (set by the backend's ``versus.mainline.axis_order``)
    so the FE doesn't maintain a parallel list. New axes appear
    automatically in the right place.
    """

    axes: dict[str, ProvenanceAxis]
    axis_order: list[str]


class ResultsBundle(pydantic.BaseModel):
    conditions: list[str]
    criteria: list[str]
    active_criterion: str | None
    gen_models: list[str]
    judge_models: list[str]
    judge_labels: dict[str, JudgeLabel]
    main_matrices: list[Matrix]
    completion_per_source: list[SourceMatrix]
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
    prefix_variants: list[PrefixVariantInfo]
    active_prefix_label: str
    provenance: ProvenanceSummary


router = APIRouter(
    prefix="/api/versus",
    tags=["versus"],
    dependencies=[Depends(require_admin)],
)


def _load_essay(essay_id: str) -> versus_essay.Essay | None:
    """Load one essay row from versus_essays as an Essay object."""
    client = versus_db.get_client()
    row = versus_db.get_essay(client, essay_id)
    if row is None:
        return None
    return versus_prepare.essay_from_db_row(row)


@router.get("/essays", response_model=list[EssayMeta])
def list_essays(include_legacy: bool = False) -> list[EssayMeta]:
    """List essays at the current essay-cache schema version.

    Old cached snapshots (pre-rename, ``schema_version < SCHEMA_VERSION``)
    are skipped by default — they often have stale prefix_config_hashes
    and would show up as a near-duplicate of the freshly-cached entry,
    confusing the inspect dropdown. Pass ``include_legacy=true`` to
    include them anyway (no UI surfaces this yet; it's a debug escape).
    """
    cfg = _cfg_cached()
    exclude = set(cfg.essays.exclude_ids) if cfg else set()
    out: list[EssayMeta] = []
    for d in _load_essay_rows():
        if d.get("id") in exclude:
            continue
        if not include_legacy and not versus_essay.is_current_schema(d):
            continue
        out.append(
            EssayMeta(
                id=d["id"],
                source_id=d.get("source_id", ""),
                title=d["title"],
                author=d.get("author", ""),
                pub_date=d.get("pub_date", ""),
                url=d.get("url", ""),
                schema_version=d.get("schema_version", 0),
            )
        )
    return out


@router.get("/essays/{essay_id}", response_model=EssayDetail)
def get_essay(essay_id: str, prefix_label: str | None = None) -> EssayDetail:
    cfg = _cfg_required()
    essay = _load_essay(essay_id)
    if not essay:
        raise HTTPException(404, f"essay {essay_id} not found")
    active_prefix_cfg = _resolve_prefix_label(cfg, prefix_label)
    task = versus_prepare.prepare(
        essay,
        n_paragraphs=active_prefix_cfg.n_paragraphs,
        include_headers=active_prefix_cfg.include_headers,
        length_tolerance=cfg.completion.length_tolerance,
    )
    completion_prompt = versus_prepare.render_prompt(
        task,
        include_headers=active_prefix_cfg.include_headers,
        tolerance=cfg.completion.length_tolerance,
    )
    if not cfg.judging.criteria:
        raise HTTPException(503, "versus config has no judging.criteria configured")
    primary_criterion = cfg.judging.criteria[0]
    judge_system, judge_user = versus_judge.render_judge_prompt(
        prefix_text="{{ PREFIX SHOWN TO JUDGE }}",
        dimension=primary_criterion,
        source_a_text="{{ CONTINUATION A }}",
        source_b_text="{{ CONTINUATION B }}",
    )
    judge_prompt_hash = versus_judge.compute_judge_prompt_hash(primary_criterion, with_tools=False)
    return EssayDetail(
        id=essay.id,
        title=essay.title,
        author=essay.author,
        pub_date=essay.pub_date,
        url=essay.url,
        markdown=essay.markdown,
        schema_version=essay.schema_version,
        prefix_config_hash=task.prefix_config_hash,
        target_words=task.target_words,
        completion_prompt=completion_prompt,
        judge_system_prompt_template=judge_system,
        judge_user_prompt_template=judge_user,
        judge_prompt_hash=judge_prompt_hash,
        criteria=list(cfg.judging.criteria),
        prefix_variants=[
            PrefixVariantInfo(
                id=p.id,
                n_paragraphs=p.n_paragraphs,
                include_headers=p.include_headers,
            )
            for p in versus_prepare.active_prefix_configs(cfg)
        ],
        active_prefix_label=active_prefix_cfg.id,
    )


@router.get("/essays/{essay_id}/sources", response_model=list[Source])
def get_essay_sources(essay_id: str, prefix_label: str | None = None) -> list[Source]:
    cfg = _cfg_required()
    essay = _load_essay(essay_id)
    if not essay:
        raise HTTPException(404, f"essay {essay_id} not found")
    active_prefix_cfg = _resolve_prefix_label(cfg, prefix_label)
    task = versus_prepare.prepare(
        essay,
        n_paragraphs=active_prefix_cfg.n_paragraphs,
        include_headers=active_prefix_cfg.include_headers,
        length_tolerance=cfg.completion.length_tolerance,
    )
    # Multiple completion rows can share the same source_id (different
    # sampling_hash); collapse to one per source_id, last-row-wins, to match
    # versus_judge.load_sources_by_essay. Server-side essay_id filter scopes
    # to ~5-10 rows instead of ~430.
    by_source: dict[str, Source] = {}
    for db_row in _per_essay_load("texts", essay.id):
        row = _legacy_text_dict(db_row)
        if row["prefix_config_hash"] != task.prefix_config_hash:
            continue
        by_source[row["source_id"]] = Source(
            source_id=row["source_id"],
            kind=row.get("source_kind", "?"),
            text=row.get("response_text") or "",
            words=row.get("response_words") or 0,
            target=row.get("target_words") or 0,
            prompt=row.get("prompt"),
            prefix_config_hash=row.get("prefix_config_hash", ""),
            sampling_hash=row.get("sampling_hash"),
            model_id=row.get("model_id"),
        )
    return sorted(by_source.values(), key=lambda s: (s.source_id != "human", s.source_id))


class EssayJudgmentsResponse(pydantic.BaseModel):
    """Judgments for a single essay at one prefix variant.

    Rows whose ``prefix_config_hash`` doesn't match the selected variant
    are filtered out of ``judgments``. They split into two buckets:

    - ``other_variant_hidden``: hash matches a *different* active prefix
      variant for this essay (e.g. you're viewing ``no_headers`` and 227
      rows belong to ``default``). Not stale — just a different cohort.
    - ``stale_hidden``: hash matches no current variant. Genuinely stale
      from essay re-import drift or a prefix-config bump.

    ``orphaned`` flags a judgment whose source_a / source_b is not
    present in versus_texts at the same prefix_hash -- the judgment
    survived the variant check but its sources did not.
    """

    judgments: list[Judgment]
    stale_hidden: int
    other_variant_hidden: int


@router.get("/essays/{essay_id}/judgments", response_model=EssayJudgmentsResponse)
def get_essay_judgments(essay_id: str, prefix_label: str | None = None) -> EssayJudgmentsResponse:
    cfg = _cfg_required()
    essay = _load_essay(essay_id)
    if not essay:
        raise HTTPException(404, f"essay {essay_id} not found")
    active_prefix_cfg = _resolve_prefix_label(cfg, prefix_label)
    task = versus_prepare.prepare(
        essay,
        n_paragraphs=active_prefix_cfg.n_paragraphs,
        include_headers=active_prefix_cfg.include_headers,
        length_tolerance=cfg.completion.length_tolerance,
    )
    # Hashes for *all* current variants, so we can tell "belongs to
    # another live variant" apart from "no longer matches any variant"
    # (genuine essay-drift staleness).
    other_variant_hashes = {
        versus_prepare.prepare(
            essay,
            n_paragraphs=p.n_paragraphs,
            include_headers=p.include_headers,
            length_tolerance=cfg.completion.length_tolerance,
        ).prefix_config_hash
        for p in versus_prepare.active_prefix_configs(cfg)
        if p.id != active_prefix_cfg.id
    }
    text_rows = _per_essay_load("texts", essay.id)
    source_index = _build_completion_source_index_from_rows(text_rows)
    judgments: list[Judgment] = []
    stale_hidden = 0
    other_variant_hidden = 0
    for db_row in _per_essay_load("judgments", essay.id):
        row = _legacy_judgment_dict(db_row)
        if row.get("prefix_config_hash") != task.prefix_config_hash:
            if row.get("verdict") is not None:
                if row.get("prefix_config_hash") in other_variant_hashes:
                    other_variant_hidden += 1
                else:
                    stale_hidden += 1
            continue
        jm = str(row.get("judge_model", ""))
        # Every row carries ``config`` post-backfill; legacy fallbacks
        # were dropped in path-B cleanup.
        row_cfg = row["config"]
        judgments.append(
            Judgment(
                judge_model=jm,
                judge_model_id=row_cfg["model"],
                config_hash=row["config_hash"],
                prompt_hash=f"p{row_cfg['prompts']['shell_hash']}",
                sampling=row_cfg["sampling"] or row.get("sampling") or {},
                criterion=row.get("criterion", ""),
                source_a=row.get("source_a", ""),
                source_b=row.get("source_b", ""),
                display_first=row.get("display_first", ""),
                display_second=row.get("display_second", ""),
                verdict=row.get("verdict"),
                winner_source=row.get("winner_source"),
                preference_label=(row.get("preference_label") or row.get("rumil_preference_label")),
                reasoning_preview=(row.get("reasoning_text") or "")[:400],
                reasoning_text=row.get("reasoning_text"),
                prompt=row.get("prompt"),
                system_prompt=row.get("system_prompt"),
                is_rumil=jm.startswith("rumil:"),
                rumil_trace_url=row.get("rumil_trace_url"),
                rumil_question_id=row.get("rumil_question_id"),
                rumil_call_id=row.get("rumil_call_id"),
                rumil_run_id=row.get("rumil_run_id"),
                rumil_cost_usd=row.get("rumil_cost_usd"),
                contamination_note=row.get("contamination_note"),
                orphaned=_is_orphaned(row, source_index),
                prefix_config_hash=row.get("prefix_config_hash", ""),
            )
        )
    judgments.sort(key=lambda j: (j.judge_model, j.criterion, j.source_a, j.source_b))
    return EssayJudgmentsResponse(
        judgments=judgments,
        stale_hidden=stale_hidden,
        other_variant_hidden=other_variant_hidden,
    )


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
        baseline = f"paraphrase:{row['config']['model']}"
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
    prefix_label: str | None = None,
) -> ResultsBundle:
    cfg = _cfg_required()

    active_prefix_cfg = _resolve_prefix_label(cfg, prefix_label)
    essays_status, current_prefix_hashes = _build_essays_status(cfg, prefix_cfg=active_prefix_cfg)

    # Single fetch per request: every downstream consumer (matrix,
    # matrix_by_source, the row loop, the source-stats loop, and the
    # orphan check) re-iterates this same data. Light projection skips
    # the multi-MB request/response JSONB blobs that no aggregation here
    # actually reads. Cached briefly so click-arounds don't re-fetch.
    all_judgments_db = _light_load("judgments")
    all_texts_db = _light_load("texts")

    data = versus_analyze.matrix(
        rows=all_judgments_db,
        include_contaminated=include_contaminated,
        current_prefix_hashes=current_prefix_hashes,
        include_stale=include_stale,
    )

    # Conditions are limited to "completion" while paraphrase generation is
    # deferred. When paraphrase comes back, restore "paraphrase" alongside
    # and re-emit the content-test matrix from analyze.content_test_matrix.
    conditions_present = sorted({k[2] for k in data}) if data else []
    conditions = [c for c in ("completion",) if c in conditions_present] or conditions_present
    criteria = cfg.judging.criteria

    present_gens = {k[0] for k in data if k[2] in conditions}
    present_judges = {k[1] for k in data if k[2] in conditions}
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

    data_by_source = versus_analyze.matrix_by_source(
        rows=all_judgments_db,
        include_contaminated=include_contaminated,
        current_prefix_hashes=current_prefix_hashes,
        include_stale=include_stale,
    )
    completion_per_source: list[SourceMatrix] = []
    for source_id in sorted(data_by_source):
        src_data = data_by_source[source_id]
        if not any(k[2] == "completion" for k in src_data):
            continue
        completion_per_source.append(
            SourceMatrix(
                source_id=source_id,
                matrix=Matrix(
                    condition="completion",
                    meta=_cond_meta("completion"),
                    cells=_cells_out(
                        versus_view.matrix_cells(
                            src_data,
                            gen_models,
                            judge_models,
                            "completion",
                            criterion,
                            keyed_by_condition=True,
                        )
                    ),
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

    # `total_judgments` is the deduped count of rows with a verdict -- this
    # is what the "N judgments" header in the UI means and what every
    # aggregate downstream (matrices, content-test, rows list) sees.
    # Counting raw rows here would over-report by every dupe + every
    # null-verdict placeholder and drift from the rendered totals.
    source_index = _build_completion_source_index_from_rows(all_texts_db)
    rows: list[JudgmentRow] = []
    matrix_input_rows: list[dict] = []
    # ``judge_model -> structured config``. Every row carries one
    # post-backfill, so ``judge_labels`` below reads from this map
    # directly without a flat-string fallback.
    config_by_judge_model: dict[str, dict] = {}
    total_judgments = 0
    rows_total_before_filter = 0
    stale_count = 0
    current_count = 0
    any_filter = bool(filter_gen or filter_judge or filter_condition or filter_criterion)
    for db_row in all_judgments_db:
        row = _legacy_judgment_dict(db_row)
        if row.get("verdict") is None:
            continue
        total_judgments += 1
        if not include_contaminated and row.get("contamination_note"):
            continue
        is_stale = versus_analyze._prefix_hash_is_stale(row, current_prefix_hashes)
        if is_stale:
            stale_count += 1
        else:
            current_count += 1
        if not include_stale and is_stale:
            continue
        rows_total_before_filter += 1
        matrix_input_rows.append(row)
        jm_for_label = str(row.get("judge_model", ""))
        if jm_for_label:
            config_by_judge_model.setdefault(jm_for_label, row["config"])
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
                prefix_config_hash=row.get("prefix_config_hash", ""),
                source_a=row["source_a"],
                source_b=row["source_b"],
                display_first=row.get("display_first") or "",
                display_second=row.get("display_second") or "",
                criterion=row["criterion"],
                judge_model=row["judge_model"],
                judge_model_id=row["config"]["model"],
                config_hash=row["config_hash"],
                verdict=row["verdict"],
                winner=row.get("winner_source") or "-",
                preference_label=(row.get("preference_label") or row.get("rumil_preference_label")),
                ts=row["ts"][:16],
                is_rumil=str(row.get("judge_model", "")).startswith("rumil:"),
                contamination_note=row.get("contamination_note"),
                stale=is_stale,
                orphaned=(not is_stale) and _is_orphaned(row, source_index),
            )
        )

    total_completions = 0
    source_stats: dict[str, dict] = {}
    for db_row in all_texts_db:
        row = _legacy_text_dict(db_row)
        total_completions += 1
        if not include_stale and versus_analyze._prefix_hash_is_stale(row, current_prefix_hashes):
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

    provenance_axes = versus_mainline.summarize_provenance(matrix_input_rows)
    current = versus_mainline.current_values_summary(cfg)
    descriptions = versus_mainline.AXIS_DESCRIPTIONS
    # Build a hash -> "essay_id / variant_id" reverse map across all
    # active prefix variants. Each (essay, variant) combination
    # contributes one current hash; collisions across variants are
    # vanishingly unlikely but we'd merge labels if they happened.
    prefix_labels: dict[str, str] = {}
    current_prefix_hash_set: set[str] = set()
    for v in versus_prepare.active_prefix_configs(cfg):
        _, hashes = _build_essays_status(cfg, prefix_cfg=v)
        for essay_id, h in hashes.items():
            current_prefix_hash_set.add(h)
            existing = prefix_labels.get(h)
            label = f"{essay_id} / {v.id}"
            prefix_labels[h] = f"{existing}, {label}" if existing else label
    current["prefix_config_hash"] = sorted(current_prefix_hash_set)

    axes_out: dict[str, ProvenanceAxis] = {}
    for axis in versus_mainline.AXES_ORDER:
        counts = provenance_axes.get(axis, {})
        if not counts and not current.get(axis):
            continue
        value_labels: dict[str, str] = {}
        if axis == "prefix_config_hash":
            for h in counts:
                if h in prefix_labels:
                    value_labels[h] = prefix_labels[h]
        axes_out[axis] = ProvenanceAxis(
            description=descriptions.get(axis, ""),
            counts=counts,
            current_values=current.get(axis, []),
            value_labels=value_labels,
        )
    provenance = ProvenanceSummary(axes=axes_out, axis_order=list(versus_mainline.AXES_ORDER))

    return ResultsBundle(
        conditions=conditions,
        criteria=criteria,
        active_criterion=criterion,
        gen_models=gen_models,
        judge_models=judge_models,
        judge_labels={
            j: JudgeLabel(**versus_analyze.label_from_config(config_by_judge_model[j]))
            for j in judge_models
        },
        main_matrices=main_matrices,
        completion_per_source=completion_per_source,
        small_grid=small_grid,
        rows=rows,
        provenance=provenance,
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
        prefix_variants=[
            PrefixVariantInfo(
                id=p.id,
                n_paragraphs=p.n_paragraphs,
                include_headers=p.include_headers,
            )
            for p in versus_prepare.active_prefix_configs(cfg)
        ],
        active_prefix_label=active_prefix_cfg.id,
    )


@router.get("/judgments/by-key", response_model=JudgmentDetail)
def get_judgment_by_key(key: str) -> JudgmentDetail:
    """Look up the verbatim row for a single judgment key.

    Used by the side-panel inspector on /versus/results so a reader can see
    the prompt + reasoning + raw response that produced a verdict. The key
    is the row's primary-key UUID, so this is a direct lookup — no scan.
    """
    _cfg_required()
    client = versus_db.get_client()
    resp = client.table("versus_judgments").select("*").eq("id", key).limit(1).execute()
    if not resp.data:
        raise HTTPException(404, f"judgment key not found: {key}")
    db_row = resp.data[0]
    if not isinstance(db_row, dict):
        raise HTTPException(500, "unexpected row shape from versus_judgments")
    row = _legacy_judgment_dict(db_row)
    jm = str(row.get("judge_model", ""))
    row_cfg = row["config"]
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
        judge_model_id=row_cfg["model"],
        config_hash=row["config_hash"],
        prompt_hash=f"p{row_cfg['prompts']['shell_hash']}",
        sampling=row_cfg["sampling"] or row.get("sampling") or {},
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
    prefix_label: str | None = None,
) -> DiagnosticsBundle:
    """Post-hoc bias / n-floor / per-essay sanity over the judgments log.

    Filters mirror /results so the pane's numbers line up with the
    matrix the operator is currently looking at.
    """
    cfg = _cfg_required()

    active_prefix_cfg = _resolve_prefix_label(cfg, prefix_label)
    _, current_prefix_hashes = _build_essays_status(cfg, prefix_cfg=active_prefix_cfg)

    titles: dict[str, str] = {d["id"]: d.get("title", d["id"]) for d in _load_essay_rows()}

    filtered_rows: list[dict] = []
    for db_row in _light_load("judgments"):
        row = _legacy_judgment_dict(db_row)
        if row.get("verdict") is None:
            continue
        if not include_contaminated and row.get("contamination_note"):
            continue
        if not include_stale and versus_analyze._prefix_hash_is_stale(row, current_prefix_hashes):
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
