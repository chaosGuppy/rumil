"""Minimal FastAPI UI for blind human judging."""

from __future__ import annotations

import datetime as dt
import itertools
import pathlib
from collections import defaultdict

import fastapi
import fastapi.responses
import fastapi.templating

from versus import analyze, config, fetch, jsonl, judge, prepare


app = fastapi.FastAPI(title="versus")
templates = fastapi.templating.Jinja2Templates(
    directory=str(pathlib.Path(__file__).parent / "templates")
)

_cfg: config.Config | None = None


def cfg() -> config.Config:
    global _cfg
    if _cfg is None:
        _cfg = config.load("config.yaml")
    return _cfg


def human_judge_id(name: str) -> str:
    return f"human:{name.strip().lower()}"


def _enumerate_pairs():
    """Yield (essay_id, prefix_hash, a, b, first, second, prefix_text, text_first, text_second, title)."""
    groups, prefix_texts = judge.load_sources_by_essay(cfg().storage.completions_log)
    # Also grab essay titles.
    titles: dict[str, str] = {}
    essays_dir = cfg().essays.cache_dir
    if essays_dir.exists():
        import json as _json
        for p in essays_dir.glob("*.json"):
            with open(p) as f:
                d = _json.load(f)
            titles[d["id"]] = d["title"]

    for (essay_id, prefix_hash), sources in groups.items():
        source_ids = sorted(sources.keys())
        if not cfg().judging.include_human_as_contestant:
            source_ids = [s for s in source_ids if s != "human"]
        if len(source_ids) < 2:
            continue
        prefix_text = prefix_texts.get((essay_id, prefix_hash), "")
        for a_id, b_id in itertools.combinations(source_ids, 2):
            src_a = judge.Source(a_id, sources[a_id])
            src_b = judge.Source(b_id, sources[b_id])
            first, second = judge.order_pair(essay_id, src_a, src_b)
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


def _already_judged(judge_model: str, criterion: str) -> set[str]:
    out: set[str] = set()
    for row in jsonl.read(cfg().storage.judgments_log):
        if row.get("judge_model") == judge_model and row.get("criterion") == criterion:
            out.add(row["key"])
    return out


def _stats_for(judge_model: str) -> dict:
    criteria = cfg().judging.criteria
    total_pairs = sum(1 for _ in _enumerate_pairs())
    counts = defaultdict(int)
    for row in jsonl.read(cfg().storage.judgments_log):
        if row.get("judge_model") == judge_model:
            counts[row.get("criterion", "")] += 1
    return {
        "total_per_criterion": total_pairs,
        "done_per_criterion": {c: counts[c] for c in criteria},
        "criteria": criteria,
    }


@app.get("/", response_class=fastapi.responses.HTMLResponse)
def index(request: fastapi.Request, name: str | None = None, criterion: str | None = None):
    if not name:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"criteria": cfg().judging.criteria},
        )
    judge_model = human_judge_id(name)
    active_criterion = criterion or cfg().judging.criteria[0]
    done = _already_judged(judge_model, active_criterion)

    all_pairs = list(_enumerate_pairs())
    next_pair = None
    total = len(all_pairs)
    done_count = 0
    for p in all_pairs:
        k = judge.judgment_key(
            p["essay_id"], p["prefix_hash"], p["a"], p["b"], active_criterion, judge_model
        )
        if k in done:
            done_count += 1
        elif next_pair is None:
            next_pair = p

    stats = _stats_for(judge_model)

    if next_pair is None:
        return templates.TemplateResponse(
            request,
            "done.html",
            {
                "name": name,
                "criterion": active_criterion,
                "criteria": cfg().judging.criteria,
                "stats": stats,
            },
        )

    return templates.TemplateResponse(
        request,
        "judge.html",
        {
            "name": name,
            "criterion": active_criterion,
            "criteria": cfg().judging.criteria,
            "criterion_desc": judge.CRITERION_PROMPTS[active_criterion],
            "pair": next_pair,
            "done_count": done_count,
            "total": total,
            "stats": stats,
        },
    )


@app.post("/judge")
def submit(
    name: str = fastapi.Form(...),
    criterion: str = fastapi.Form(...),
    essay_id: str = fastapi.Form(...),
    prefix_hash: str = fastapi.Form(...),
    a: str = fastapi.Form(...),
    b: str = fastapi.Form(...),
    first_source: str = fastapi.Form(...),
    second_source: str = fastapi.Form(...),
    verdict: str = fastapi.Form(...),   # "A" | "B" | "tie" | "skip"
    note: str = fastapi.Form(""),
):
    judge_model = human_judge_id(name)
    if verdict == "skip":
        return fastapi.responses.RedirectResponse(
            f"/?name={name}&criterion={criterion}&_skip={essay_id}|{a}|{b}", status_code=303
        )
    winner_source = None
    if verdict == "A":
        winner_source = first_source
    elif verdict == "B":
        winner_source = second_source
    elif verdict == "tie":
        winner_source = "tie"
    else:
        raise fastapi.HTTPException(400, f"bad verdict: {verdict}")

    k = judge.judgment_key(essay_id, prefix_hash, a, b, criterion, judge_model)
    row = {
        "key": k,
        "essay_id": essay_id,
        "prefix_config_hash": prefix_hash,
        "source_a": a,
        "source_b": b,
        "display_first": first_source,
        "display_second": second_source,
        "criterion": criterion,
        "judge_model": judge_model,
        "verdict": verdict,
        "winner_source": winner_source,
        "reasoning_text": note,
        "prompt": None,
        "ts": dt.datetime.utcnow().isoformat() + "Z",
        "duration_s": None,
        "raw_response": None,
    }
    jsonl.append(cfg().storage.judgments_log, row)
    return fastapi.responses.RedirectResponse(
        f"/?name={name}&criterion={criterion}", status_code=303
    )


def _load_essays() -> list[fetch.Essay]:
    import json as _json
    essays: list[fetch.Essay] = []
    essays_dir = cfg().essays.cache_dir
    if not essays_dir.exists():
        return essays
    for p in sorted(essays_dir.glob("*.json")):
        with open(p) as f:
            d = _json.load(f)
        essays.append(
            fetch.Essay(
                id=d["id"],
                url=d["url"],
                title=d["title"],
                author=d["author"],
                pub_date=d["pub_date"],
                blocks=[fetch.Block(**b) for b in d["blocks"]],
                markdown=d.get("markdown", ""),
                schema_version=d.get("schema_version", 0),
            )
        )
    return essays


@app.get("/inspect", response_class=fastapi.responses.HTMLResponse)
def inspect(request: fastapi.Request, essay: str | None = None):
    essays = _load_essays()
    essay_options = [(e.id, e.title) for e in essays]
    if not essays:
        return templates.TemplateResponse(
            request, "inspect.html",
            {"essays": essay_options, "selected": None, "view": None},
        )
    selected_id = essay or essays[0].id
    selected = next((e for e in essays if e.id == selected_id), essays[0])

    task = prepare.prepare(
        selected,
        n_paragraphs=cfg().prefix.n_paragraphs,
        include_headers=cfg().prefix.include_headers,
    )
    completion_prompt = prepare.render_prompt(
        task,
        include_headers=cfg().prefix.include_headers,
        tolerance=cfg().completion.length_tolerance,
    )
    judge_prompt_template = judge.render_judge_prompt(
        prefix_text="{{ PREFIX SHOWN TO JUDGE }}",
        criterion=cfg().judging.criteria[0],
        source_a_text="{{ CONTINUATION A }}",
        source_b_text="{{ CONTINUATION B }}",
    )
    from versus import paraphrase as _para
    paraphrase_prompt_template = _para.PARAPHRASE_INSTRUCTIONS.replace(
        "{markdown}", "{{ FULL ESSAY MARKDOWN }}"
    )

    # Pull completions for this essay+prefix_config so we can show each source side-by-side.
    sources = []
    for row in jsonl.read(cfg().storage.completions_log):
        if row["essay_id"] != selected.id or row["prefix_config_hash"] != task.prefix_config_hash:
            continue
        sources.append({
            "source_id": row["source_id"],
            "kind": row.get("source_kind", "?"),
            "text": row.get("response_text") or "",
            "words": row.get("response_words") or 0,
            "target": row.get("target_words") or 0,
        })
    sources.sort(key=lambda s: (s["source_id"] != "human", s["source_id"]))

    view = {
        "essay_markdown": selected.markdown,
        "completion_prompt": completion_prompt,
        "judge_prompt_template": judge_prompt_template,
        "paraphrase_prompt_template": paraphrase_prompt_template,
        "target_words": task.target_words,
        "prefix_config_hash": task.prefix_config_hash,
        "criteria": cfg().judging.criteria,
        "sources": sources,
    }
    return templates.TemplateResponse(
        request, "inspect.html",
        {"essays": essay_options, "selected": selected, "view": view},
    )


def _cell_color(pct: float) -> str:
    """Linear gradient: 0 → orange (model preferred), 50 → light gray, 100 → green (human preferred)."""
    if pct <= 0.5:
        t = pct / 0.5            # 0..1 over 0..50
        r, g, b = 255, int(111 + (238 - 111) * t), int(67 + (238 - 67) * t)
    else:
        t = (pct - 0.5) / 0.5    # 0..1 over 50..100
        r = int(238 - (238 - 110) * t)
        g = int(238 - (238 - 199) * t)
        b = int(238 - (238 - 120) * t)
    return f"rgb({r},{g},{b})"


def _text_color(pct: float) -> str:
    # dark text everywhere except the darkest ends
    return "#111"


@app.get("/results", response_class=fastapi.responses.HTMLResponse)
def results(
    request: fastapi.Request,
    criterion: str | None = None,
):
    data = analyze.matrix(cfg().storage.judgments_log)
    conditions_present = sorted({k[2] for k in data}) if data else []
    conditions = ["completion", "paraphrase"]
    conditions = [c for c in conditions if c in conditions_present] or conditions_present
    criteria = cfg().judging.criteria
    active_crit = criterion  # None means "average"

    # Axis order follows config (flash, mini, 5.4, ...) rather than alphabetical.
    present_gens = {k[0] for k in data if k[2] in conditions}
    present_judges = {k[1] for k in data if k[2] in conditions}
    gen_models = [m.id for m in cfg().completion.models if m.id in present_gens]
    gen_models += sorted(g for g in present_gens if g not in gen_models)  # orphans last
    judge_models = [j for j in cfg().judging.models if j in present_judges]
    judge_models += sorted(j for j in present_judges if j not in judge_models)

    def build_cell(gen, jmod, cond, crit_filter):
        hs, ns = 0.0, 0
        for (g, j, c, cr), (pct, n) in data.items():
            if g == gen and j == jmod and c == cond:
                if crit_filter and cr != crit_filter:
                    continue
                hs += pct * n
                ns += n
        pct = (hs / ns) if ns else None
        return {
            "pct": pct,
            "n": ns,
            "bg": _cell_color(pct) if pct is not None else "#f4f4f0",
            "fg": _text_color(pct) if pct is not None else "#999",
        }

    # Condition metadata for the UI — title, pair description, value semantics.
    COND_META = {
        "completion": {
            "title": "vs human · from-scratch continuation",
            "pair": "pair: (human continuation, G's from-scratch continuation) — judged by J",
            "cell_meaning": "cell: % J picks human. High → J prefers the real continuation over G's new one.",
            "value_picks": "human",
        },
        "paraphrase": {
            "title": "vs human · same-model paraphrase",
            "pair": "pair: (human continuation, G's rewrite of the human continuation) — judged by J",
            "cell_meaning": "cell: % J picks human. Content is held constant; this isolates style preference.",
            "value_picks": "human",
        },
    }
    main_matrices = [
        {
            "condition": cond,
            "meta": COND_META[cond],
            "cells": {(g, j): build_cell(g, j, cond, active_crit)
                      for g in gen_models for j in judge_models},
        }
        for cond in conditions
    ]

    # Content-test matrix: (paraphrase:J, completion:G) judged by J.
    # Same axes: gen_model (rows) × judge_model (cols). Judge is the paraphrase author.
    content_data = analyze.content_test_matrix(cfg().storage.judgments_log)
    def build_content_cell(gen, jmod, crit_filter):
        hs, ns = 0.0, 0
        for (g, j, cr), (pct, n) in content_data.items():
            if g == gen and j == jmod:
                if crit_filter and cr != crit_filter:
                    continue
                hs += pct * n
                ns += n
        pct = (hs / ns) if ns else None
        return {
            "pct": pct,
            "n": ns,
            "bg": _cell_color(pct) if pct is not None else "#f4f4f0",
            "fg": _text_color(pct) if pct is not None else "#999",
        }
    content_matrix = {
        "condition": "content-test",
        "meta": {
            "title": "style-controlled · content test",
            "pair": "pair: (G's from-scratch continuation, J's rewrite of the human) — judged by J",
            "cell_meaning": "cell: % J picks its own human-content-baseline. On the diagonal (G=J), style is held at J; off-diagonal mixes styles.",
            "value_picks": "J's paraphrase (= human content in J's voice)",
        },
        "cells": {(g, j): build_content_cell(g, j, active_crit)
                  for g in gen_models for j in judge_models},
    }

    # grid rows = condition, cols = criterion (used for the facet grid)
    small_grid = [
        {
            "condition": cond,
            "per_crit": [
                {
                    "criterion": crit,
                    "cells": {(g, j): build_cell(g, j, cond, crit)
                              for g in gen_models for j in judge_models},
                }
                for crit in criteria
            ],
        }
        for cond in conditions
    ]
    # Also facet the content-test per criterion.
    small_grid.append({
        "condition": "content-test",
        "per_crit": [
            {
                "criterion": crit,
                "cells": {(g, j): build_content_cell(g, j, crit)
                          for g in gen_models for j in judge_models},
            }
            for crit in criteria
        ],
    })

    # raw judgments grouped for explorer
    rows = []
    for row in jsonl.read(cfg().storage.judgments_log):
        if row.get("verdict") is None:
            continue
        rows.append(
            {
                "essay_id": row["essay_id"],
                "source_a": row["source_a"],
                "source_b": row["source_b"],
                "criterion": row["criterion"],
                "judge_model": row["judge_model"],
                "verdict": row["verdict"],
                "winner": row.get("winner_source") or "-",
                "ts": row["ts"][:16],
            }
        )

    total_judgments = sum(1 for _ in jsonl.read(cfg().storage.judgments_log))
    total_completions = sum(1 for _ in jsonl.read(cfg().storage.completions_log))

    # per-source word-count summary (avg words, avg delta from target)
    source_stats: dict[str, dict] = {}
    for row in jsonl.read(cfg().storage.completions_log):
        sid = row["source_id"]
        stats = source_stats.setdefault(sid, {"n": 0, "words": 0, "delta": 0.0})
        stats["n"] += 1
        stats["words"] += row.get("response_words") or 0
        if row.get("target_words"):
            stats["delta"] += (row["response_words"] - row["target_words"]) / row["target_words"]
    sources_summary = []
    for sid, s in sorted(source_stats.items(), key=lambda x: (x[0] != "human", x[0])):
        n = s["n"]
        sources_summary.append({
            "source_id": sid,
            "n": n,
            "avg_words": (s["words"] // n) if n else 0,
            "avg_delta_pct": (s["delta"] / n * 100) if n else 0.0,
        })

    return templates.TemplateResponse(
        request,
        "results.html",
        {
            "conditions": conditions,
            "criteria": criteria,
            "active_crit": active_crit,
            "gen_models": gen_models,
            "judge_models": judge_models,
            "main_matrices": main_matrices + [content_matrix],
            "small_grid": small_grid,
            "rows": rows,
            "total_judgments": total_judgments,
            "total_completions": total_completions,
            "sources_summary": sources_summary,
        },
    )


def main() -> None:
    import uvicorn
    uvicorn.run("versus.ui:app", host="127.0.0.1", port=cfg().ui.port, reload=False)


if __name__ == "__main__":
    main()
