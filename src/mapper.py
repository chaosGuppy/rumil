"""
Generate a visual HTML map of the research tree for a question.
"""
import json
from datetime import datetime
from html import escape
from pathlib import Path

from database import DB
from models import Page, PageType

PAGES_DIR = Path(__file__).parent.parent / "pages"
MAPS_DIR = PAGES_DIR / "maps"


def _confidence_color(confidence: float) -> str:
    """10-shade HSL gradient from red (low) to green (high). Input is 0-5 scale."""
    clamped = max(0.0, min(1.0, confidence / 5.0))
    bucket = min(9, int(clamped * 10))
    hue = bucket * (120 / 9)
    return f"hsl({hue:.0f}, 55%, 93%)"


def _stars(strength: float) -> str:
    """0-5 strength mapped to ★☆ rating out of 5."""
    filled = max(0, min(5, round(strength)))
    return "★" * filled + "☆" * (5 - filled)


def _find_page_file(page: Page) -> Path | None:
    short_id = page.id[:8]
    matches = list((PAGES_DIR / "research").glob(f"*{short_id}*.md"))
    return matches[0] if matches else None


def _rel_path(page_file: Path) -> str:
    """Relative path from maps/ to the page file."""
    return "../" + "/".join(page_file.relative_to(PAGES_DIR).parts)


def _budget_select(select_id: str) -> str:
    return (f'<select id="{select_id}" class="budget-select">'
            '<option value="1">1</option>'
            '<option value="3">3</option>'
            '<option value="5" selected>5</option>'
            '<option value="10">10</option>'
            '<option value="20">20</option>'
            '</select>')


def _render_consideration(claim: Page, link, parent_question_id: str) -> str:
    color = _confidence_color(claim.epistemic_status)
    stars = _stars(link.strength)
    direction = link.direction.value if link.direction else "neutral"
    direction_icon = {"supports": "↑", "opposes": "↓", "neutral": "→"}.get(direction, "→")

    words = claim.summary.split()
    short = " ".join(words[:30]) + ("…" if len(words) > 30 else "")
    epistemic = (f"{claim.epistemic_status:.1f}/5 confidence"
                 + (f" — {escape(claim.epistemic_type)}" if claim.epistemic_type else ""))

    page_file = _find_page_file(claim)
    source_link = (f'<a class="source-link" href="{_rel_path(page_file)}">View source →</a>'
                   if page_file else "")

    sel_id = f"b-{claim.id[:8]}"
    # Escape the summary for safe JS string embedding
    js_summary = claim.summary.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    btn = (f'{_budget_select(sel_id)}'
           f'<button class="inv-btn" onclick="copyCmd(this, '
           f'\'python main.py --add-question &quot;{js_summary[:80]}&quot; '
           f'--parent {parent_question_id} --budget \' + '
           f'document.getElementById(\'{sel_id}\').value)">'
           f'Dig into this</button>')

    return f"""<div class="consideration {direction}" style="background:{color}">
  <details>
    <summary>
      <span class="stars" title="Strength {link.strength:.1f}">{stars}</span>
      <span class="dir-icon" title="{direction}">{direction_icon}</span>
      <span class="summary-text">{escape(short)}</span>
    </summary>
    <div class="expanded">
      <p class="epistemic">{epistemic}</p>
      <p class="body">{escape(claim.content)}</p>
      <div class="action-row">{source_link}<span class="action-gap"></span>{btn}</div>
    </div>
  </details>
</div>"""


def _render_judgement(j: Page, index: int, total: int) -> str:
    color = _confidence_color(j.epistemic_status)
    label = f"Judgement {index + 1}/{total}" if total > 1 else "Judgement"
    extra = json.loads(j.extra) if j.extra else {}
    deps = extra.get("key_dependencies", "")
    sens = extra.get("sensitivity_analysis", "")
    meta = ""
    if deps:
        meta += f'<p class="meta"><strong>Dependencies:</strong> {escape(deps)}</p>'
    if sens:
        meta += f'<p class="meta"><strong>Sensitivity:</strong> {escape(sens)}</p>'

    return f"""<div class="judgement" style="background:{color}">
  <details>
    <summary>
      <span class="j-label">{label}</span>
      <span class="conf-badge">{j.epistemic_status:.1f}/5</span>
      <span class="summary-text">{escape(j.summary[:100])}</span>
    </summary>
    <div class="expanded">
      <p class="epistemic">{j.epistemic_status:.1f}/5 confidence — {escape(j.epistemic_type)}</p>
      <p class="body">{escape(j.content)}</p>
      {meta}
    </div>
  </details>
</div>"""


def _render_question(question_id: str, db: DB, depth: int = 0) -> str:
    question = db.get_page(question_id)
    if not question:
        return ""

    considerations = db.get_considerations_for_question(question_id)
    supports = sorted([(p, l) for p, l in considerations
                       if l.direction and l.direction.value == "supports"],
                      key=lambda x: x[1].strength, reverse=True)
    opposes  = sorted([(p, l) for p, l in considerations
                       if l.direction and l.direction.value == "opposes"],
                      key=lambda x: x[1].strength, reverse=True)
    neutral  = sorted([(p, l) for p, l in considerations
                       if not l.direction or l.direction.value == "neutral"],
                      key=lambda x: x[1].strength, reverse=True)

    judgements = sorted(db.get_judgements_for_question(question_id), key=lambda j: j.created_at)
    children   = db.get_child_questions(question_id)

    # Stats line
    parts = []
    if considerations:
        parts.append(f"{len(considerations)} consideration{'s' if len(considerations) != 1 else ''}")
    if judgements:
        parts.append(f"{len(judgements)} judgement{'s' if len(judgements) != 1 else ''}")
    if children:
        parts.append(f"{len(children)} sub-question{'s' if len(children) != 1 else ''}")
    stats = ", ".join(parts) if parts else "no research yet"

    # Judgements block
    j_html = ""
    if judgements:
        j_html = '<div class="section"><h4>Judgements</h4>'
        for i, j in enumerate(judgements):
            j_html += _render_judgement(j, i, len(judgements))
        j_html += "</div>"

    # Considerations block
    c_html = ""
    if considerations:
        c_html = '<div class="section">'
        if supports:
            c_html += "<h4>Supporting</h4>"
            for p, l in supports:
                c_html += _render_consideration(p, l, question_id)
        if opposes:
            c_html += "<h4>Opposing</h4>"
            for p, l in opposes:
                c_html += _render_consideration(p, l, question_id)
        if neutral:
            c_html += "<h4>Neutral / contextual</h4>"
            for p, l in neutral:
                c_html += _render_consideration(p, l, question_id)
        c_html += "</div>"

    # Sub-questions block
    ch_html = ""
    if children:
        ch_html = '<div class="section"><h4>Sub-questions</h4>'
        for child in children:
            ch_html += _render_question(child.id, db, depth=depth + 1)
        ch_html += "</div>"

    q_sel_id = f"qb-{question_id[:8]}"
    q_btn = (f'<div class="q-action-row">{_budget_select(q_sel_id)}'
             f'<button class="inv-btn" onclick="copyCmd(this, '
             f'\'python main.py --continue {question_id} --budget \' + '
             f'document.getElementById(\'{q_sel_id}\').value)">'
             f'Investigate further</button></div>')

    hn = min(depth + 2, 6)
    return f"""<div class="q-node depth-{depth}">
  <h{hn} class="q-heading">{escape(question.summary)}</h{hn}>
  <p class="q-stats">{stats}</p>
  {q_btn}
  {j_html}{c_html}{ch_html}
</div>"""


_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 15px; line-height: 1.55; color: #1a1a1a;
  background: #f4f4ef; padding: 2rem;
}
h1 { font-size: 1.5rem; margin-bottom: 0.25rem; }
h2 { font-size: 1.25rem; margin-bottom: 0.6rem; color: #222; }
h3 { font-size: 1.1rem;  margin-bottom: 0.5rem; color: #333; }
h4 { font-size: 0.78rem; text-transform: uppercase; letter-spacing: .06em;
     color: #999; margin: 0.9rem 0 0.35rem; }
h5, h6 { font-size: 0.95rem; margin-bottom: 0.35rem; color: #444; }

.subtitle { font-size: 0.9rem; color: #888; margin-bottom: 1.5rem; }

.q-node {
  background: #fff; border: 1px solid #ddd; border-radius: 8px;
  padding: 1.1rem 1.4rem; margin-bottom: 0.9rem;
}
.q-node.depth-0 { border-left: 4px solid #4a90d9; }
.q-node.depth-1 { border-left: 4px solid #7ab5e8; margin-left: 1.5rem; }
.q-node.depth-2 { border-left: 4px solid #a8cff0; margin-left: 3rem;   }
.q-node.depth-3 { border-left: 4px solid #c5dff5; margin-left: 4.5rem; }
.q-node.depth-4 { border-left: 4px solid #ddeef8; margin-left: 6rem;   }

.q-heading { margin-bottom: 0.15rem; }
.q-stats   { font-size: 0.78rem; color: #aaa; margin-bottom: 0.2rem; }

.section { margin-top: 0.5rem; }

.consideration, .judgement {
  border-radius: 5px; margin-bottom: 0.35rem;
  padding: 0.35rem 0.6rem; border: 1px solid rgba(0,0,0,.07);
}
.consideration.supports { border-left: 3px solid #4caf50; }
.consideration.opposes  { border-left: 3px solid #f44336; }
.consideration.neutral  { border-left: 3px solid #9e9e9e; }
.judgement              { border-left: 3px solid #9c27b0; }

details > summary {
  cursor: pointer; list-style: none;
  display: flex; align-items: baseline; gap: 0.35rem;
}
details > summary::-webkit-details-marker { display: none; }
details[open] > summary { margin-bottom: 0.35rem; }

.stars    { color: #c89200; font-size: 0.82rem; flex-shrink: 0; }
.dir-icon { font-size: 0.78rem; color: #888; flex-shrink: 0; }
.summary-text { font-size: 0.88rem; }

.j-label    { font-size: 0.72rem; background: #9c27b0; color: #fff;
              border-radius: 3px; padding: 0 5px; flex-shrink: 0; }
.conf-badge { font-size: 0.75rem; color: #777; flex-shrink: 0; }

.expanded {
  padding-top: 0.5rem; border-top: 1px solid rgba(0,0,0,.07);
}
.epistemic { font-size: 0.76rem; color: #888; font-style: italic; margin-bottom: 0.35rem; }
.body      { font-size: 0.88rem; margin-bottom: 0.4rem; white-space: pre-wrap; }
.meta      { font-size: 0.8rem;  color: #666; margin-top: 0.3rem; }

.source-link { font-size: 0.76rem; color: #4a90d9; text-decoration: none; }
.source-link:hover { text-decoration: underline; }

.footer { font-size: 0.72rem; color: #ccc; margin-top: 2rem; text-align: center; }

.action-row   { display: flex; align-items: center; gap: 0.4rem; margin-top: 0.5rem; }
.q-action-row { display: flex; align-items: center; gap: 0.4rem; margin: 0.25rem 0 0.4rem; }
.action-gap   { flex: 1; }

.budget-select {
  font-size: 0.75rem; border: 1px solid #ccc; border-radius: 4px;
  padding: 2px 4px; background: #fff; color: #444; cursor: pointer;
}
.inv-btn {
  font-size: 0.75rem; padding: 3px 9px; border-radius: 4px; border: 1px solid #bbb;
  background: #f0f0f0; color: #333; cursor: pointer; transition: background 0.15s;
}
.inv-btn:hover   { background: #e0e8f5; border-color: #4a90d9; color: #1a1a1a; }
.inv-btn.copied  { background: #d4edda; border-color: #4caf50; color: #2e7d32; }
"""


def generate_map(question_id: str, db: DB) -> Path:
    """Generate an HTML research map and return the file path."""
    MAPS_DIR.mkdir(parents=True, exist_ok=True)

    question = db.get_page(question_id)
    if not question:
        raise ValueError(f"Question {question_id} not found")

    tree_html = _render_question(question_id, db)

    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    slug = "".join(c if c.isalnum() or c in " -" else "" for c in question.summary[:50])
    slug = slug.strip().replace(" ", "-").lower()
    output_path = MAPS_DIR / f"{timestamp}-{slug}.html"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Research Map: {escape(question.summary[:60])}</title>
  <style>{_CSS}</style>
  <script>
  function copyCmd(btn, cmd) {{
    navigator.clipboard.writeText(cmd).then(function() {{
      var orig = btn.textContent;
      btn.textContent = 'Copied!';
      btn.classList.add('copied');
      setTimeout(function() {{
        btn.textContent = orig;
        btn.classList.remove('copied');
      }}, 1800);
    }}).catch(function() {{
      prompt('Copy this command:', cmd);
    }});
  }}
  </script>
</head>
<body>
  <p class="subtitle">Research Map</p>
  <h1>{escape(question.summary)}</h1>
  {tree_html}
  <p class="footer">Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC</p>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")
    return output_path
