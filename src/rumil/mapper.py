"""
Generate a visual HTML map of the research tree for a question.
"""

from datetime import UTC, datetime
from html import escape
from pathlib import Path

from rumil.database import DB
from rumil.models import Page

PAGES_DIR = Path(__file__).parent.parent.parent / "pages"
MAPS_DIR = PAGES_DIR / "maps"


def _confidence_color(credence: int) -> str:
    """HSL gradient from red (low) to green (high). Input is 1-9 credence scale."""
    clamped = max(0.0, min(1.0, (credence - 1) / 8.0))
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
    return (
        f'<select id="{select_id}" class="budget-select">'
        '<option value="1">1</option>'
        '<option value="3">3</option>'
        '<option value="5" selected>5</option>'
        '<option value="10">10</option>'
        '<option value="20">20</option>'
        "</select>"
    )


def _render_consideration(claim: Page, link, parent_question_id: str) -> str:
    color = _confidence_color(claim.credence or 5)
    stars = _stars(link.strength)
    words = claim.headline.split()
    short = " ".join(words[:30]) + ("…" if len(words) > 30 else "")
    epistemic = f"C{claim.credence}/R{claim.robustness}" if claim.credence is not None else ""

    page_file = _find_page_file(claim)
    source_link = (
        f'<a class="source-link" href="{_rel_path(page_file)}">View source →</a>'
        if page_file
        else ""
    )

    sel_id = f"b-{claim.id[:8]}"
    # Escape the summary for safe JS string embedding
    js_summary = (
        claim.headline.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    )
    btn = (
        f"{_budget_select(sel_id)}"
        '<button class="inv-btn" onclick="copyCmd(this, '
        f"'python main.py --add-question &quot;{js_summary[:80]}&quot; "
        f"--parent {parent_question_id} --budget ' + "
        f"document.getElementById('{sel_id}').value)\">"
        "Dig into this</button>"
    )

    return (
        f'<div class="consideration" style="background:{color}">\n'
        '  <details>\n'
        '    <summary>\n'
        f'      <span class="stars" title="Strength {link.strength:.1f}">{stars}</span>\n'
        f'      <span class="summary-text">{escape(short)}</span>\n'
        '    </summary>\n'
        '    <div class="expanded">\n'
        f'      <p class="epistemic">{epistemic}</p>\n'
        f'      <p class="body">{escape(claim.content)}</p>\n'
        f'      <div class="action-row">{source_link}<span class="action-gap"></span>{btn}</div>\n'
        '    </div>\n'
        '  </details>\n'
        '</div>'
    )


def _render_judgement(j: Page, index: int, total: int) -> str:
    color = _confidence_color(j.credence or 5)
    label = f"Judgement {index + 1}/{total}" if total > 1 else "Judgement"
    extra = j.extra or {}
    deps = extra.get("key_dependencies", "")
    sens = extra.get("sensitivity_analysis", "")
    meta = ""
    if deps:
        meta += f'<p class="meta"><strong>Dependencies:</strong> {escape(deps)}</p>'
    if sens:
        meta += f'<p class="meta"><strong>Sensitivity:</strong> {escape(sens)}</p>'

    return (
        f'<div class="judgement" style="background:{color}">\n'
        '  <details>\n'
        '    <summary>\n'
        f'      <span class="j-label">{label}</span>\n'
        f'      <span class="conf-badge">C{j.credence}/R{j.robustness}</span>\n'
        f'      <span class="summary-text">{escape(j.headline[:100])}</span>\n'
        '    </summary>\n'
        '    <div class="expanded">\n'
        f'      <p class="epistemic">Credence: {j.credence}/9 | Robustness: {j.robustness}/5</p>\n'
        f'      <p class="body">{escape(j.content)}</p>\n'
        f'      {meta}\n'
        '    </div>\n'
        '  </details>\n'
        '</div>'
    )


async def _render_question(
    question_id: str,
    db: DB,
    depth: int = 0,
    _visited: set[str] | None = None,
) -> str:
    if _visited is None:
        _visited = set()
    if question_id in _visited:
        return ""
    _visited = _visited | {question_id}

    question = await db.get_page(question_id)
    if not question:
        return ""

    considerations = await db.get_considerations_for_question(question_id)
    considerations_sorted = sorted(
        considerations, key=lambda x: x[1].strength, reverse=True
    )

    judgements = sorted(
        await db.get_judgements_for_question(question_id), key=lambda j: j.created_at
    )
    children = await db.get_child_questions(question_id)

    # Stats line
    parts = []
    if considerations:
        parts.append(
            f"{len(considerations)} consideration{'s' if len(considerations) != 1 else ''}"
        )
    if judgements:
        parts.append(
            f"{len(judgements)} judgement{'s' if len(judgements) != 1 else ''}"
        )
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
    if considerations_sorted:
        c_html = '<div class="section"><h4>Considerations</h4>'
        for p, l in considerations_sorted:
            c_html += _render_consideration(p, l, question_id)
        c_html += "</div>"

    # Sub-questions block
    ch_html = ""
    if children:
        ch_html = '<div class="section"><h4>Sub-questions</h4>'
        for child in children:
            ch_html += await _render_question(child.id, db, depth=depth + 1, _visited=_visited)
        ch_html += "</div>"

    q_sel_id = f"qb-{question_id[:8]}"
    q_btn = (
        f'<div class="q-action-row">{_budget_select(q_sel_id)}'
        '<button class="inv-btn" onclick="copyCmd(this, '
        f"'python main.py --continue {question_id} --budget ' + "
        f"document.getElementById('{q_sel_id}').value)\">"
        "Investigate further</button></div>"
    )

    hn = min(depth + 2, 6)
    return (
        f'<div class="q-node depth-{depth}">\n'
        f'  <h{hn} class="q-heading">{escape(question.headline)}</h{hn}>\n'
        f'  <p class="q-stats">{stats}</p>\n'
        f'  {q_btn}\n'
        f'  {j_html}{c_html}{ch_html}\n'
        '</div>'
    )


_CSS = (
    "* { box-sizing: border-box; margin: 0; padding: 0; }\n"
    "body {"
    "  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;"
    "  font-size: 15px; line-height: 1.55; color: #1a1a1a;"
    "  background: #f4f4ef; padding: 2rem;"
    "}\n"
    "h1 { font-size: 1.5rem; margin-bottom: 0.25rem; }\n"
    "h2 { font-size: 1.25rem; margin-bottom: 0.6rem; color: #222; }\n"
    "h3 { font-size: 1.1rem;  margin-bottom: 0.5rem; color: #333; }\n"
    "h4 { font-size: 0.78rem; text-transform: uppercase; letter-spacing: .06em;"
    "     color: #999; margin: 0.9rem 0 0.35rem; }\n"
    "h5, h6 { font-size: 0.95rem; margin-bottom: 0.35rem; color: #444; }\n"
    ".subtitle { font-size: 0.9rem; color: #888; margin-bottom: 1.5rem; }\n"
    ".q-node {"
    "  background: #fff; border: 1px solid #ddd; border-radius: 8px;"
    "  padding: 1.1rem 1.4rem; margin-bottom: 0.9rem;"
    "}\n"
    ".q-node.depth-0 { border-left: 4px solid #4a90d9; }\n"
    ".q-node.depth-1 { border-left: 4px solid #7ab5e8; margin-left: 1.5rem; }\n"
    ".q-node.depth-2 { border-left: 4px solid #a8cff0; margin-left: 3rem;   }\n"
    ".q-node.depth-3 { border-left: 4px solid #c5dff5; margin-left: 4.5rem; }\n"
    ".q-node.depth-4 { border-left: 4px solid #ddeef8; margin-left: 6rem;   }\n"
    ".q-heading { margin-bottom: 0.15rem; }\n"
    ".q-stats   { font-size: 0.78rem; color: #aaa; margin-bottom: 0.2rem; }\n"
    ".section { margin-top: 0.5rem; }\n"
    ".consideration, .judgement {"
    "  border-radius: 5px; margin-bottom: 0.35rem;"
    "  padding: 0.35rem 0.6rem; border: 1px solid rgba(0,0,0,.07);"
    "}\n"
    ".consideration          { border-left: 3px solid #9e9e9e; }\n"
    ".judgement              { border-left: 3px solid #9c27b0; }\n"
    "details > summary {"
    "  cursor: pointer; list-style: none;"
    "  display: flex; align-items: baseline; gap: 0.35rem;"
    "}\n"
    "details > summary::-webkit-details-marker { display: none; }\n"
    "details[open] > summary { margin-bottom: 0.35rem; }\n"
    ".stars    { color: #c89200; font-size: 0.82rem; flex-shrink: 0; }\n"
    ".summary-text { font-size: 0.88rem; }\n"
    ".j-label    { font-size: 0.72rem; background: #9c27b0; color: #fff;"
    "              border-radius: 3px; padding: 0 5px; flex-shrink: 0; }\n"
    ".conf-badge { font-size: 0.75rem; color: #777; flex-shrink: 0; }\n"
    ".expanded {"
    "  padding-top: 0.5rem; border-top: 1px solid rgba(0,0,0,.07);"
    "}\n"
    ".epistemic { font-size: 0.76rem; color: #888; font-style: italic; margin-bottom: 0.35rem; }\n"
    ".body      { font-size: 0.88rem; margin-bottom: 0.4rem; white-space: pre-wrap; }\n"
    ".meta      { font-size: 0.8rem;  color: #666; margin-top: 0.3rem; }\n"
    ".source-link { font-size: 0.76rem; color: #4a90d9; text-decoration: none; }\n"
    ".source-link:hover { text-decoration: underline; }\n"
    ".footer { font-size: 0.72rem; color: #ccc; margin-top: 2rem; text-align: center; }\n"
    ".action-row   { display: flex; align-items: center; gap: 0.4rem; margin-top: 0.5rem; }\n"
    ".q-action-row { display: flex; align-items: center; gap: 0.4rem; margin: 0.25rem 0 0.4rem; }\n"
    ".action-gap   { flex: 1; }\n"
    ".budget-select {"
    "  font-size: 0.75rem; border: 1px solid #ccc; border-radius: 4px;"
    "  padding: 2px 4px; background: #fff; color: #444; cursor: pointer;"
    "}\n"
    ".inv-btn {"
    "  font-size: 0.75rem; padding: 3px 9px; border-radius: 4px; border: 1px solid #bbb;"
    "  background: #f0f0f0; color: #333; cursor: pointer; transition: background 0.15s;"
    "}\n"
    ".inv-btn:hover   { background: #e0e8f5; border-color: #4a90d9; color: #1a1a1a; }\n"
    ".inv-btn.copied  { background: #d4edda; border-color: #4caf50; color: #2e7d32; }\n"
)


async def generate_map(question_id: str, db: DB) -> Path:
    """Generate an HTML research map and return the file path."""
    MAPS_DIR.mkdir(parents=True, exist_ok=True)

    question = await db.get_page(question_id)
    if not question:
        raise ValueError(f"Question {question_id} not found")

    tree_html = await _render_question(question_id, db)

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    slug = "".join(c if c.isalnum() or c in " -" else "" for c in question.headline[:50])
    slug = slug.strip().replace(" ", "-").lower()
    output_path = MAPS_DIR / f"{timestamp}-{slug}.html"

    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    html = (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '  <meta charset="UTF-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f'  <title>Research Map: {escape(question.headline[:60])}</title>\n'
        f'  <style>{_CSS}</style>\n'
        '  <script>\n'
        '  function copyCmd(btn, cmd) {\n'
        '    navigator.clipboard.writeText(cmd).then(function() {\n'
        '      var orig = btn.textContent;\n'
        "      btn.textContent = 'Copied!';\n"
        "      btn.classList.add('copied');\n"
        '      setTimeout(function() {\n'
        '        btn.textContent = orig;\n'
        "        btn.classList.remove('copied');\n"
        '      }, 1800);\n'
        '    }).catch(function() {\n'
        "      prompt('Copy this command:', cmd);\n"
        '    });\n'
        '  }\n'
        '  </script>\n'
        '</head>\n'
        '<body>\n'
        '  <p class="subtitle">Research Map</p>\n'
        f'  <h1>{escape(question.headline)}</h1>\n'
        f'  {tree_html}\n'
        f'  <p class="footer">Generated {generated_at} UTC</p>\n'
        '</body>\n'
        '</html>'
    )

    output_path.write_text(html, encoding="utf-8")
    return output_path
