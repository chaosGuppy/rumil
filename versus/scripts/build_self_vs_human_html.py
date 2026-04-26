"""Bake a standalone HTML viewer for the no_headers vs default
self-vs-human cells across the 12 forethought essays.

Per essay:
  - human held-out remainder under each variant (so reader can compare)
  - per-model section, both variants stacked:
      * model continuation (with expandable continuation prompt)
      * self-vs-human judgment (with expandable system + user prompt,
        full reasoning, verdict, preference label)

Single-file output: HTML + embedded CSS + tiny vanilla JS for the
essay dropdown. Open the file in a browser; no server required.

  uv run --with-editable .. python scripts/build_self_vs_human_html.py
"""

from __future__ import annotations

import html
import json
import pathlib
import sys

VERSUS_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VERSUS_ROOT / "src"))

from versus.essay import Block, Essay  # noqa: E402

from versus import config, prepare  # noqa: E402
from versus import jsonl as versus_jsonl  # noqa: E402

FORETHOUGHT_IDS = [
    "forethought__ai-for-ai-for-epistemics",
    "forethought__ai-for-decision-advice",
    "forethought__ai-impacts-on-epistemics-the-good-the-bad-and-the-ugly",
    "forethought__ai-should-sometimes-be-proactively-prosocial",
    "forethought__broad-timelines",
    "forethought__design-sketches-angels-on-the-shoulder",
    "forethought__design-sketches-collective-epistemics",
    "forethought__design-sketches-defense-favoured-coordination-tech",
    "forethought__design-sketches-tools-for-strategic-awareness",
    "forethought__moral-public-goods-are-a-big-deal-for-whether-we-get-a-good-future",
    "forethought__should-we-lock-in-post-agi-agreements-under-uncertainty",
    "forethought__the-importance-of-ai-character",
]

MODEL_ORDER = [
    ("google/gemini-3-flash-preview", "flash"),
    ("google/gemini-3.1-pro-preview", "3.1-pro"),
    ("openai/gpt-5.4-mini", "gpt-5.4-mini"),
    ("openai/gpt-5.4", "gpt-5.4"),
    ("anthropic/claude-sonnet-4-6", "sonnet"),
    ("anthropic/claude-opus-4-7", "opus"),
]

VARIANT_ORDER = ["default", "no_headers"]


def load_essay(eid: str) -> Essay:
    p = VERSUS_ROOT / "data" / "essays" / f"{eid}.json"
    d = json.loads(p.read_text())
    return Essay(
        id=d["id"],
        source_id=d["source_id"],
        url=d.get("url", ""),
        title=d.get("title", ""),
        author=d.get("author", ""),
        pub_date=d.get("pub_date", ""),
        blocks=[Block(**b) for b in d["blocks"]],
        markdown=d.get("markdown", ""),
        image_count=d.get("image_count", 0),
        schema_version=d.get("schema_version", 0),
    )


def main() -> None:
    cfg = config.load(VERSUS_ROOT / "config.yaml")
    variants_by_id = {p.id: p for p in prepare.active_prefix_configs(cfg)}
    missing = [v for v in VARIANT_ORDER if v not in variants_by_id]
    if missing:
        sys.exit(f"config is missing required prefix variants: {missing}")

    essays = {eid: load_essay(eid) for eid in FORETHOUGHT_IDS}
    # (essay_id, variant_id) -> {"task": PreparedTask, "prompt": str}
    rendered: dict[tuple[str, str], dict] = {}
    for eid, essay in essays.items():
        for vid in VARIANT_ORDER:
            v = variants_by_id[vid]
            task = prepare.prepare(
                essay,
                n_paragraphs=v.n_paragraphs,
                include_headers=v.include_headers,
                length_tolerance=cfg.completion.length_tolerance,
            )
            prompt = prepare.render_prompt(
                task,
                include_headers=v.include_headers,
                tolerance=cfg.completion.length_tolerance,
            )
            rendered[(eid, vid)] = {"task": task, "prompt": prompt}

    # variant hash → variant id
    hash_to_variant: dict[str, str] = {}
    # (essay_id, variant_id) → variant prefix_config_hash
    essay_variant_hash: dict[tuple[str, str], str] = {}
    for (eid, vid), payload in rendered.items():
        h = payload["task"].prefix_config_hash
        hash_to_variant[h] = vid
        essay_variant_hash[(eid, vid)] = h

    # Pull rows. Index completions by (essay, variant, source_id) →
    # latest row; judgments by (essay, variant, judge_base, model) for the
    # M-vs-human self-judgment.
    completions: dict[tuple[str, str, str], dict] = {}
    for r in versus_jsonl.read(VERSUS_ROOT / "data" / "completions.jsonl"):
        eid = r.get("essay_id")
        h = r.get("prefix_config_hash")
        vid = hash_to_variant.get(h)
        if vid is None or eid not in essays:
            continue
        completions[(eid, vid, r["source_id"])] = r

    judgments: dict[tuple[str, str, str], dict] = {}
    for r in versus_jsonl.read_dedup(VERSUS_ROOT / "data" / "judgments.jsonl"):
        if r.get("verdict") is None:
            continue
        eid = r.get("essay_id")
        h = r.get("prefix_config_hash")
        vid = hash_to_variant.get(h)
        if vid is None or eid not in essays:
            continue
        sources = {r.get("source_a"), r.get("source_b")}
        if "human" not in sources:
            continue
        other = (sources - {"human"}).pop()
        if other not in dict(MODEL_ORDER):
            continue
        base = (r.get("judge_model") or "").split(":p")[0]
        if base != other:
            continue  # only self-judgments
        judgments[(eid, vid, other)] = r

    # Aggregate self-vs-human "% picks human" per (variant, model) for the
    # summary table at the bottom.
    tallies: dict[tuple[str, str], dict[str, int]] = {
        (vid, m): {"human": 0, "self": 0, "tie": 0, "n": 0}
        for vid in VARIANT_ORDER
        for m, _ in MODEL_ORDER
    }
    for (eid, vid, model_id), r in judgments.items():
        ws = r.get("winner_source")
        t = tallies[(vid, model_id)]
        t["n"] += 1
        if ws == "human":
            t["human"] += 1
        elif ws == "tie":
            t["tie"] += 1
        else:
            t["self"] += 1

    out_path = VERSUS_ROOT / "data" / "self_vs_human_forethought.html"
    out_path.write_text(_render_html(essays, rendered, completions, judgments, tallies))
    print(f"wrote {out_path}")


def _render_html(
    essays: dict[str, Essay],
    rendered: dict[tuple[str, str], dict],
    completions: dict[tuple[str, str, str], dict],
    judgments: dict[tuple[str, str, str], dict],
    tallies: dict[tuple[str, str], dict[str, int]],
) -> str:
    essay_options = "\n".join(
        f'    <option value="{html.escape(eid)}">{html.escape(essays[eid].title)}</option>'
        for eid in FORETHOUGHT_IDS
    )
    model_options = '    <option value="all">all models</option>\n' + "\n".join(
        f'    <option value="{html.escape(mid)}">{html.escape(short)} ({html.escape(mid)})</option>'
        for mid, short in MODEL_ORDER
    )

    panels = "\n".join(
        _render_essay_panel(eid, essays[eid], rendered, completions, judgments)
        for eid in FORETHOUGHT_IDS
    )
    summary_table = _render_summary_table(tallies)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>versus · forethought self-vs-human · default vs no_headers</title>
<style>
  body {{
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    color: #222; max-width: 1200px; margin: 0 auto; padding: 24px;
  }}
  header {{ border-bottom: 1px solid #ddd; padding-bottom: 12px; margin-bottom: 18px; }}
  h1 {{ font-size: 20px; margin: 0 0 6px; font-weight: 500; }}
  h2 {{ font-size: 18px; margin: 18px 0 8px; font-weight: 500; }}
  h3 {{ font-size: 15px; margin: 16px 0 6px; font-weight: 600; }}
  h4 {{ font-size: 13px; margin: 10px 0 4px; font-weight: 500; color: #555; text-transform: uppercase; letter-spacing: 0.04em; }}
  .muted {{ color: #777; }}
  .row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 10px; }}
  .cond {{ border: 1px solid #e0e0e0; border-radius: 4px; padding: 12px; background: #fcfcfa; }}
  .cond-head {{ display: flex; align-items: baseline; gap: 8px; margin-bottom: 6px; }}
  .pill {{ font-size: 11px; padding: 2px 7px; border-radius: 3px; background: #eee; color: #333; font-weight: 500; }}
  .pill.default {{ background: #e7eef9; }}
  .pill.no_headers {{ background: #f4ecdb; }}
  details {{ margin: 6px 0; }}
  details summary {{ cursor: pointer; color: #555; font-size: 12px; user-select: none; }}
  details summary:hover {{ color: #222; }}
  pre {{
    white-space: pre-wrap; word-wrap: break-word; font: 13px/1.5 Georgia, "Times New Roman", serif;
    background: #fafafa; border: 1px solid #e8e8e8; border-radius: 3px;
    padding: 10px 12px; margin: 6px 0; max-height: 60vh; overflow: auto;
  }}
  pre.prompt {{ font: 12px/1.4 ui-monospace, Menlo, monospace; max-height: 40vh; }}
  .verdict {{ margin: 8px 0; }}
  .verdict .winner {{ font-weight: 600; }}
  .winner.human {{ color: #2a7d3a; }}
  .winner.self  {{ color: #b04a16; }}
  .winner.tie   {{ color: #555; }}
  .meta {{ font-size: 12px; color: #888; }}
  select {{ font-size: 14px; padding: 4px 8px; min-width: 280px; }}
  .controls {{ display: flex; gap: 16px; flex-wrap: wrap; align-items: baseline; margin-top: 4px; }}
  .essay-panel {{ display: none; }}
  .essay-panel.active {{ display: block; }}
  .model-section {{ display: block; }}
  .model-section.hidden {{ display: none; }}
  code {{ font: 12px ui-monospace, Menlo, monospace; background: #f0f0f0; padding: 1px 4px; border-radius: 2px; }}
  table.summary {{ border-collapse: collapse; margin-top: 8px; }}
  table.summary th, table.summary td {{ padding: 6px 12px; text-align: left; border-bottom: 1px solid #eee; }}
  table.summary th {{ background: #f6f6f4; font-weight: 600; font-size: 12px; }}
  table.summary td {{ font: 13px ui-monospace, Menlo, monospace; }}
  .summary-section {{ margin-top: 40px; padding-top: 18px; border-top: 1px solid #ccc; }}
</style>
</head>
<body>
<header>
  <h1>forethought self-vs-human · default (3-para + headings) vs no_headers (1-para, no headings)</h1>
  <p class="muted">For each model M, judge = M scoring (M's continuation, human held-out). 12 forethought.org essays, two prefix conditions.</p>
  <div class="controls">
    <label>Essay:&nbsp;
      <select id="essay-select" onchange="showEssay(this.value)">
{essay_options}
      </select>
    </label>
    <label>Model:&nbsp;
      <select id="model-select" onchange="showModel(this.value)">
{model_options}
      </select>
    </label>
  </div>
</header>
<main id="panels">
{panels}
</main>

<section class="summary-section">
  <h2>% picks human in self-judgments</h2>
  <p class="muted">Model M judges (M, human). Higher = judge prefers human. Ties split ½. Same data as the per-essay panels above, aggregated.</p>
  {summary_table}
</section>

<script>
  function showEssay(eid) {{
    document.querySelectorAll('.essay-panel').forEach(p => p.classList.remove('active'));
    var el = document.querySelector('.essay-panel[data-essay="' + CSS.escape(eid) + '"]');
    if (el) el.classList.add('active');
    syncHash();
  }}
  function showModel(mid) {{
    document.querySelectorAll('.model-section').forEach(s => {{
      s.classList.toggle('hidden', mid !== 'all' && s.dataset.model !== mid);
    }});
    syncHash();
  }}
  function syncHash() {{
    var eid = document.getElementById('essay-select').value;
    var mid = document.getElementById('model-select').value;
    var parts = ['e=' + encodeURIComponent(eid)];
    if (mid !== 'all') parts.push('m=' + encodeURIComponent(mid));
    history.replaceState(null, '', '#' + parts.join('&'));
  }}
  (function() {{
    var hash = (location.hash || '').slice(1);
    var params = new URLSearchParams(hash);
    var eSel = document.getElementById('essay-select');
    var mSel = document.getElementById('model-select');
    var eVal = params.get('e');
    var mVal = params.get('m');
    if (eVal && Array.from(eSel.options).some(o => o.value === eVal)) eSel.value = eVal;
    if (mVal && Array.from(mSel.options).some(o => o.value === mVal)) mSel.value = mVal;
    showEssay(eSel.value);
    showModel(mSel.value);
  }})();
</script>
</body>
</html>
"""


def _render_summary_table(tallies: dict[tuple[str, str], dict[str, int]]) -> str:
    rows = []
    for model_id, short in MODEL_ORDER:
        cells = [f"<td>{html.escape(short)}</td>"]
        for vid in VARIANT_ORDER:
            t = tallies[(vid, model_id)]
            if t["n"] == 0:
                cells.append("<td>—</td>")
            else:
                pct = (t["human"] + 0.5 * t["tie"]) / t["n"] * 100
                cells.append(f'<td>{pct:.1f}% <span class="muted">(n={t["n"]})</span></td>')
        rows.append("<tr>" + "".join(cells) + "</tr>")
    headers = "<th>model</th>" + "".join(f"<th>{html.escape(v)}</th>" for v in VARIANT_ORDER)
    return (
        '<table class="summary"><thead><tr>'
        + headers
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _render_essay_panel(
    eid: str,
    essay: Essay,
    rendered: dict[tuple[str, str], dict],
    completions: dict[tuple[str, str, str], dict],
    judgments: dict[tuple[str, str, str], dict],
) -> str:
    title = html.escape(essay.title)
    url = html.escape(essay.url)

    human_blocks = []
    for vid in VARIANT_ORDER:
        task = rendered[(eid, vid)]["task"]
        human_blocks.append(
            f'<details><summary><span class="pill {vid}">{vid}</span> human held-out remainder · '
            f"{len(task.remainder_markdown.split())} words</summary>"
            f"<pre>{html.escape(task.remainder_markdown)}</pre></details>"
        )

    model_sections = []
    for model_id, short in MODEL_ORDER:
        model_sections.append(
            _render_model_section(eid, model_id, short, rendered, completions, judgments)
        )

    return f"""
<div class="essay-panel" data-essay="{html.escape(eid)}">
  <h2>{title}</h2>
  <p class="meta"><a href="{url}" target="_blank" rel="noreferrer">source</a></p>
  <details><summary>original essay (normalized markdown)</summary>
    <pre>{html.escape(essay.markdown)}</pre>
  </details>
  <h3>Human</h3>
  {"".join(human_blocks)}
  {"".join(model_sections)}
</div>
"""


def _render_model_section(
    eid: str,
    model_id: str,
    short: str,
    rendered: dict[tuple[str, str], dict],
    completions: dict[tuple[str, str, str], dict],
    judgments: dict[tuple[str, str, str], dict],
) -> str:
    cells = []
    for vid in VARIANT_ORDER:
        prompt = rendered[(eid, vid)]["prompt"]
        comp = completions.get((eid, vid, model_id))
        judg = judgments.get((eid, vid, model_id))
        cells.append(_render_condition_cell(vid, prompt, comp, judg, model_id))
    return f"""
<section class="model-section" data-model="{html.escape(model_id)}">
  <h3>{html.escape(short)} <code>{html.escape(model_id)}</code></h3>
  <div class="row">
{"".join(cells)}
  </div>
</section>
"""


def _render_condition_cell(
    vid: str, prompt: str, comp: dict | None, judg: dict | None, model_id: str
) -> str:
    if comp is None:
        comp_html = '<p class="muted">(no continuation row — refused or never run)</p>'
    else:
        text = comp.get("response_text") or ""
        words = comp.get("response_words") or len(text.split())
        target = comp.get("target_words") or 0
        # Prefer the row's stored prompt — that's what the model actually
        # received. Fall back to a freshly-rendered prompt only when the
        # row has none (legacy rows or the human baseline).
        stored_prompt = comp.get("prompt")
        actual_prompt = stored_prompt or prompt
        prompt_label = (
            "continuation prompt (verbatim from row)"
            if stored_prompt
            else "continuation prompt (rebuilt — row has none)"
        )
        comp_html = (
            f"<details><summary>{html.escape(prompt_label)}</summary>"
            f'<pre class="prompt">{html.escape(actual_prompt)}</pre></details>'
            f'<p class="meta">{words} words (target ~{target})</p>'
            f"<pre>{html.escape(text)}</pre>"
        )

    if judg is None:
        judg_html = '<p class="muted">(no self-vs-human judgment row)</p>'
    else:
        winner = judg.get("winner_source") or "?"
        if winner == "human":
            wclass = "human"
        elif winner == "tie":
            wclass = "tie"
        else:
            wclass = "self"
        verdict = judg.get("verdict") or "?"
        pref = judg.get("preference_label") or judg.get("rumil_preference_label") or ""
        sys_p = judg.get("system_prompt") or ""
        usr_p = judg.get("prompt") or ""
        reason = judg.get("reasoning_text") or ""
        judg_html = f"""
<h4>self vs human · judge = {html.escape(model_id)}</h4>
<details><summary>judge system prompt</summary><pre class="prompt">{html.escape(sys_p)}</pre></details>
<details><summary>judge user prompt (full essay prefix + both continuations)</summary><pre class="prompt">{html.escape(usr_p)}</pre></details>
<div class="verdict">verdict: <strong>{html.escape(verdict)}</strong> · winner: <span class="winner {wclass}">{html.escape(winner)}</span>{" · label: " + html.escape(pref) if pref else ""}</div>
<details open><summary>reasoning</summary><pre>{html.escape(reason)}</pre></details>
"""

    return f"""
<div class="cond">
  <div class="cond-head"><span class="pill {vid}">{vid}</span></div>
  {comp_html}
  {judg_html}
</div>
"""


if __name__ == "__main__":
    main()
