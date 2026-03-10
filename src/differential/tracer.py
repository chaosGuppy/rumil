"""Execution tracing: capture call events and generate HTML visualizations."""

import json
import os
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from differential.database import DB
from differential.models import Call, CallType

PAGES_DIR = Path(__file__).parent.parent.parent / "pages"
TRACES_DIR = PAGES_DIR / "traces"


TRACING_ENABLED = not os.environ.get("DIFFERENTIAL_TEST_MODE")


class CallTrace:
    """Accumulates trace events during a call and persists them to the DB."""

    def __init__(self, call_id: str, db: DB):
        self.call_id = call_id
        self.db = db
        self.events: list[dict] = []
        self._enabled = TRACING_ENABLED

    def record(self, event: str, data: dict | None = None) -> None:
        if not self._enabled:
            return
        entry: dict = {
            "event": event,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if data:
            entry["data"] = data
        self.events.append(entry)

    def save(self) -> None:
        if not self._enabled:
            return
        if self.events:
            self.db.save_call_trace(self.call_id, self.events)
            self.events = []


_UUID_LEN = 36


def _looks_like_uuid(s: str) -> bool:
    return isinstance(s, str) and len(s) == _UUID_LEN and s.count("-") == 4


def _short(s: str) -> str:
    return s[:8] if _looks_like_uuid(s) else s


def _render_page_chip(page_id: str, db: DB) -> str:
    """Render a page ID as a clickable chip with summary and expandable content."""
    short = _short(page_id)
    page = db.get_page(page_id) if _looks_like_uuid(page_id) else None
    if not page:
        return f'<span class="page-chip">[{escape(short)}]</span>'
    summary = escape(page.summary)
    content = escape(page.content)
    ptype = page.page_type.value
    return (
        f'<details class="page-chip-detail">'
        f'<summary class="page-chip {ptype}">'
        f'<span class="chip-id">[{short}]</span> '
        f'<span class="chip-summary">{summary}</span>'
        f"</summary>"
        f'<div class="chip-content">'
        f'<p class="chip-meta">{ptype} | '
        f"{page.epistemic_status:.1f}/5 {escape(page.epistemic_type)}</p>"
        f'<p class="chip-body">{content}'
        f"</p>"
        f"</div>"
        f"</details>"
    )


def _render_page_list(page_ids: list, db: DB) -> str:
    """Render a list of page IDs as chips."""
    if not page_ids:
        return '<span class="empty">none</span>'
    parts = [_render_page_chip(pid, db) for pid in page_ids]
    return '<div class="page-list">' + "".join(parts) + "</div>"


_MOVE_LABELS = {
    "CREATE_CLAIM": ("claim", "creates"),
    "CREATE_QUESTION": ("question", "creates"),
    "CREATE_JUDGEMENT": ("judgement", "creates"),
    "CREATE_CONCEPT": ("concept", "creates"),
    "CREATE_WIKI_PAGE": ("wiki", "creates"),
    "LINK_CONSIDERATION": ("link", "links"),
    "LINK_CHILD_QUESTION": ("link", "links"),
    "LINK_RELATED": ("link", "links"),
    "SUPERSEDE_PAGE": ("supersede", "supersedes"),
    "FLAG_FUNNINESS": ("flag", "flags"),
    "REPORT_DUPLICATE": ("flag", "flags"),
    "PROPOSE_HYPOTHESIS": ("hypothesis", "creates"),
    "LOAD_PAGE": ("load", "loads"),
}


def _render_move(move_data: dict, db: DB) -> str:
    """Render a single move with its payload details."""
    move_type = move_data.get("type", "?")
    label_class, _ = _MOVE_LABELS.get(move_type, ("unknown", "?"))

    # Build key-value pairs for display, excluding 'type' and very long fields
    display_fields: list[tuple[str, str]] = []
    for k, v in move_data.items():
        if k == "type":
            continue
        sv = str(v)
        # Render page ID references as chips
        if (
            k
            in (
                "claim_id",
                "question_id",
                "from_page_id",
                "to_page_id",
                "page_id",
                "page_id_a",
                "page_id_b",
                "old_page_id",
                "parent_question_id",
            )
            and isinstance(v, str)
            and v
        ):
            page = db.get_page(v) if _looks_like_uuid(v) else None
            if page:
                sv = f'[{_short(v)}] "{page.summary}"'
            else:
                sv = f"[{_short(v)}]"
        display_fields.append((k, sv))

    fields_html = ""
    if display_fields:
        fields_html = '<div class="move-fields">'
        for k, v in display_fields:
            fields_html += (
                f'<div class="move-field">'
                f'<span class="field-key">{escape(k)}:</span> '
                f'<span class="field-val">{escape(v)}</span>'
                f"</div>"
            )
        fields_html += "</div>"

    return (
        f'<div class="move-item">'
        f"<details>"
        f"<summary>"
        f'<span class="move-badge {label_class}">{escape(move_type)}</span>'
        f'<span class="move-summary">{escape(_move_one_liner(move_data, db))}</span>'
        f"</summary>"
        f"{fields_html}"
        f"</details>"
        f"</div>"
    )


def _move_one_liner(move_data: dict, db: DB | None = None) -> str:
    """Generate a short one-line summary for a move."""
    summary = move_data.get("summary", "")
    if summary:
        return summary
    if "hypothesis" in move_data:
        return move_data["hypothesis"]
    if "claim_id" in move_data or "from_page_id" in move_data:
        direction = move_data.get("direction", "")
        return f"{direction}" if direction else "link"
    if "page_id" in move_data:
        pid = move_data["page_id"]
        if db:
            page = db.get_page(pid) if _looks_like_uuid(pid) else None
            if page:
                return f"[{_short(pid)}] {page.summary}"
        return f"[{_short(pid)}]"
    if "note" in move_data:
        return move_data["note"]
    return ""


def _render_event_context_built(ev: dict, db: DB) -> str:
    data = ev.get("data", {})
    working = data.get("working_context_page_ids", [])
    preloaded = data.get("preloaded_page_ids", [])
    budget = data.get("budget")
    source = data.get("source_page_id")

    html = '<div class="ev-section">'
    html += '<span class="ev-label">Working context:</span>'
    html += _render_page_list(working, db)
    if preloaded:
        html += '<span class="ev-label">Pre-loaded (from dispatch):</span>'
        html += _render_page_list(preloaded, db)
    if source:
        html += '<span class="ev-label">Source:</span>'
        html += _render_page_chip(source, db)
    if budget is not None:
        html += f'<span class="ev-label">Budget: {budget}</span>'
    html += "</div>"
    return html


def _render_event_pages_loaded(ev: dict, db: DB) -> str:
    data = ev.get("data", {})
    page_ids = data.get("page_ids", [])
    if not page_ids:
        return '<div class="ev-section"><span class="ev-label">No pages loaded</span></div>'
    html = '<div class="ev-section">'
    html += _render_page_list(page_ids, db)
    html += "</div>"
    return html


def _render_event_moves(ev: dict, db: DB) -> str:
    data = ev.get("data", {})
    moves = data.get("moves", [])
    created = data.get("created_page_ids", [])

    moves = [m for m in moves if isinstance(m, dict) and m.get("type") != "LOAD_PAGE"]

    if not moves:
        return '<div class="ev-section"><span class="ev-label">Moves:</span> none</div>'

    html = (
        f'<div class="ev-section"><span class="ev-label">Moves ({len(moves)}):</span>'
    )
    html += '<div class="moves-list">'
    for m in moves:
        html += _render_move(m, db)
    html += "</div>"
    if created:
        html += '<span class="ev-label">Created pages:</span>'
        html += _render_page_list(created, db)
    html += "</div>"
    return html


def _render_event_dispatches(ev: dict, db: DB) -> str:  # noqa: ARG001
    data = ev.get("data", {})
    dispatches = data.get("dispatches", [])
    if not dispatches:
        return ""
    html = (
        f'<div class="ev-section">'
        f'<span class="ev-label">Planned dispatches ({len(dispatches)}):</span>'
        f'<div class="dispatch-plan">'
    )
    for i, d in enumerate(dispatches):
        ct = d.get("call_type", "?")
        reason = d.get("reason", "")
        budget = d.get("budget", "")
        html += (
            f'<div class="dispatch-item">'
            f'<span class="dispatch-index">{i + 1}.</span> '
            f'<span class="move-badge {ct}">{escape(ct)}</span>'
        )
        if budget:
            html += f' <span class="dispatch-budget">budget={budget}</span>'
        if reason:
            html += f' <span class="dispatch-reason">— {escape(reason)}</span>'
        html += "</div>"
    html += "</div></div>"
    return html


def _render_event_review(ev: dict, db: DB) -> str:  # noqa: ARG001
    data = ev.get("data", {})
    fruit = data.get("remaining_fruit", "")
    conf = data.get("confidence", "")
    return (
        f'<div class="ev-section review-section">'
        f'<span class="ev-label">Review:</span> '
        f"fruit={fruit}, confidence={conf}"
        f"</div>"
    )


def _render_event_generic(ev: dict) -> str:
    """Fallback: render as JSON."""
    data = ev.get("data", {})
    if not data:
        return ""
    return (
        '<div class="ev-section"><pre class="ev-data">'
        + escape(json.dumps(data, indent=2, default=str))
        + "</pre></div>"
    )


_EVENT_RENDERERS = {
    "context_built": _render_event_context_built,
    "phase1_loaded": _render_event_pages_loaded,
    "phase2_loaded": _render_event_pages_loaded,
    "moves_executed": _render_event_moves,
    "dispatches_planned": _render_event_dispatches,
    "review_complete": _render_event_review,
}


def _render_event(ev: dict, db: DB) -> str:
    name = ev.get("event", "")
    ts = ev.get("ts", "")
    if ts:
        try:
            dt = datetime.fromisoformat(ts)
            ts = dt.strftime("%H:%M:%S")
        except ValueError:
            pass

    renderer = _EVENT_RENDERERS.get(name)
    if renderer:
        body_html = renderer(ev, db)
    else:
        body_html = _render_event_generic(ev)

    return (
        '<div class="event">'
        '<div class="ev-header">'
        f'<span class="ev-name">{escape(name)}</span>'
        f'<span class="ev-ts">{escape(ts)}</span>'
        "</div>"
        f"{body_html}"
        "</div>"
    )


_CALL_COLORS = {
    CallType.SCOUT: "#e3f2fd",
    CallType.ASSESS: "#f3e5f5",
    CallType.PRIORITIZATION: "#fff3e0",
    CallType.INGEST: "#e8f5e9",
}


def _call_color(call_type: CallType) -> str:
    return _CALL_COLORS.get(call_type, "#f5f5f5")


def _render_call_node(call: Call, db: DB, depth: int = 0) -> str:
    trace_events = db.get_call_trace(call.id)
    children = db.get_child_calls(call.id)
    color = _call_color(call.call_type)
    short_id = call.id[:8]

    scope_label = ""
    if call.scope_page_id:
        scope_label = db.page_label(call.scope_page_id)

    status_badge = call.status.value
    duration = ""
    if call.created_at and call.completed_at:
        delta = call.completed_at - call.created_at
        secs = int(delta.total_seconds())
        duration = f" ({secs}s)"

    # Render dispatch links with anchors to child calls
    dispatch_links = ""
    dispatch_events = [
        e for e in trace_events if e.get("event") == "dispatches_planned"
    ]
    if dispatch_events:
        planned = dispatch_events[0].get("data", {}).get("dispatches", [])
        if planned:
            executed_map: dict[int, dict] = {}
            for e in trace_events:
                if e.get("event") == "dispatch_executed":
                    idx = e.get("data", {}).get("index")
                    if idx is not None:
                        executed_map[idx] = e.get("data", {})

            dispatch_links = (
                '<div class="dispatch-nav"><strong>Dispatches:</strong><ol>'
            )
            for i, d in enumerate(planned):
                ct = d.get("call_type", "?")
                reason = d.get("reason", "")
                ex = executed_map.get(i)
                if ex:
                    child_id = ex.get("child_call_id", "")
                    if child_id:
                        dispatch_links += (
                            f'<li><a href="#call-{child_id[:8]}" class="dispatch-link">'
                            f'<span class="move-badge {ct}">{escape(ct)}</span></a>'
                            f" {escape(reason)}</li>"
                        )
                    else:
                        dispatch_links += (
                            f'<li><span class="move-badge {ct}">{escape(ct)}</span>'
                            f" {escape(reason)}</li>"
                        )
                else:
                    dispatch_links += (
                        f'<li><span class="skipped">'
                        f'<span class="move-badge {ct}">{escape(ct)}</span> (skipped)'
                        f"</span> {escape(reason)}</li>"
                    )
            dispatch_links += "</ol></div>"

    # Render non-dispatch events
    events_html = ""
    skip_events = {"dispatches_planned", "dispatch_executed"}
    displayable = [e for e in trace_events if e.get("event") not in skip_events]
    if displayable:
        events_html = '<div class="events">'
        for ev in displayable:
            events_html += _render_event(ev, db)
        events_html += "</div>"

    # Review from call record
    review_html = ""
    if call.review_json:
        fruit = call.review_json.get("remaining_fruit", "")
        conf = call.review_json.get("confidence_in_output", "")
        assessment = call.review_json.get("self_assessment", "")
        if fruit or conf or assessment:
            review_html = (
                '<div class="review">'
                f"<strong>Review:</strong> fruit={fruit}, confidence={conf}"
            )
            if assessment:
                review_html += f"<br><em>{escape(str(assessment))}</em>"
            review_html += "</div>"

    children_html = ""
    if children:
        children_html = '<div class="children">'
        for child in children:
            children_html += _render_call_node(child, db, depth=depth + 1)
        children_html += "</div>"

    return (
        f'<div class="call-node" id="call-{short_id}" style="background:{color};">'
        f"<details{'' if depth > 1 else ' open'}>"
        f"<summary>"
        f'<span class="call-type">{escape(call.call_type.value)}</span>'
        f'<span class="call-id">[{short_id}]</span>'
        f'<span class="call-status {status_badge}">{status_badge}{duration}</span>'
        f'<span class="call-scope">{escape(scope_label)}</span>'
        f"</summary>"
        f'<div class="call-body">'
        f"{dispatch_links}"
        f"{events_html}"
        f"{review_html}"
        f"{children_html}"
        f"</div>"
        f"</details>"
        f"</div>"
    )


_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 14px; line-height: 1.5; color: #1a1a1a;
  background: #f4f4ef; padding: 2rem;
}
h1 { font-size: 1.4rem; margin-bottom: 0.25rem; }
.subtitle { font-size: 0.85rem; color: #888; margin-bottom: 1.5rem; }

.call-node {
  border: 1px solid #ddd; border-radius: 6px;
  padding: 0.6rem 0.9rem; margin: 0.5rem 0 0.5rem 1.2rem;
}

details > summary {
  cursor: pointer; list-style: none;
  display: flex; align-items: baseline; gap: 0.4rem; flex-wrap: wrap;
}
details > summary::-webkit-details-marker { display: none; }
details[open] > summary { margin-bottom: 0.4rem; }

.call-type {
  font-weight: 600; font-size: 0.82rem; text-transform: uppercase;
  letter-spacing: 0.04em;
}
.call-id { font-size: 0.75rem; color: #888; font-family: monospace; }
.call-status {
  font-size: 0.7rem; padding: 1px 6px; border-radius: 3px;
  background: #e0e0e0; color: #555;
}
.call-status.complete { background: #c8e6c9; color: #2e7d32; }
.call-status.failed { background: #ffcdd2; color: #c62828; }
.call-status.running { background: #fff9c4; color: #f57f17; }
.call-scope { font-size: 0.82rem; color: #555; }

.call-body { padding-top: 0.3rem; }

/* Events */
.events { margin: 0.4rem 0; }
.event {
  padding: 0.3rem 0; border-bottom: 1px solid rgba(0,0,0,.06);
}
.ev-header {
  display: flex; align-items: baseline; gap: 0.4rem; margin-bottom: 0.2rem;
}
.ev-name {
  font-size: 0.78rem; font-weight: 500; color: #333;
  background: #e8e8e8; padding: 0 5px; border-radius: 3px;
}
.ev-ts { font-size: 0.7rem; color: #aaa; font-family: monospace; }
.ev-data {
  width: 100%; font-size: 0.72rem; background: #fafafa;
  border: 1px solid #eee; border-radius: 4px; padding: 0.3rem 0.5rem;
  margin-top: 0.2rem; overflow-x: auto; white-space: pre-wrap;
}
.ev-section { margin: 0.2rem 0 0.2rem 0.5rem; font-size: 0.82rem; }
.ev-label {
  display: block;
  font-size: 0.75rem; font-weight: 600; color: #666;
  margin-top: 0.4rem;
}
.empty { color: #bbb; font-style: italic; font-size: 0.78rem; }

/* Page chips */
.page-list { display: flex; flex-wrap: wrap; gap: 0.25rem; margin: 0.2rem 0; }
.page-chip {
  font-size: 0.76rem; padding: 1px 6px; border-radius: 4px;
  border: 1px solid #ccc; background: #fff; cursor: pointer;
  display: inline-flex; align-items: baseline; gap: 0.25rem;
}
.page-chip.question { border-color: #90caf9; background: #e3f2fd; }
.page-chip.claim { border-color: #a5d6a7; background: #e8f5e9; }
.page-chip.judgement { border-color: #ce93d8; background: #f3e5f5; }
.page-chip.concept { border-color: #ffcc80; background: #fff3e0; }
.page-chip.source { border-color: #bcaaa4; background: #efebe9; }
.chip-id { font-family: monospace; color: #888; font-size: 0.7rem; }
.chip-summary { color: #333; }
.page-chip-detail { display: inline; }
.page-chip-detail[open] { display: block; margin: 0.3rem 0; }
.chip-content {
  margin: 0.3rem 0 0.3rem 0.5rem; padding: 0.4rem 0.6rem;
  background: #fafafa; border: 1px solid #eee; border-radius: 4px;
  font-size: 0.78rem;
}
.chip-meta { font-size: 0.72rem; color: #888; margin-bottom: 0.3rem; }
.chip-body { white-space: pre-wrap; color: #444; }

/* Moves */
.moves-list { margin: 0.2rem 0 0.2rem 0.3rem; }
.move-item { margin: 0.15rem 0; }
.move-item > details > summary {
  display: flex; align-items: baseline; gap: 0.3rem;
  font-size: 0.8rem; cursor: pointer;
}
.move-badge {
  font-size: 0.68rem; font-weight: 600; padding: 0 5px; border-radius: 3px;
  background: #e0e0e0; color: #555; text-transform: uppercase;
  letter-spacing: 0.03em; white-space: nowrap;
}
.move-badge.creates, .move-badge.claim, .move-badge.question,
.move-badge.judgement, .move-badge.concept, .move-badge.wiki { background: #c8e6c9; color: #2e7d32; }
.move-badge.hypothesis { background: #fff9c4; color: #f57f17; }
.move-badge.link, .move-badge.links { background: #bbdefb; color: #1565c0; }
.move-badge.supersede { background: #ffcdd2; color: #c62828; }
.move-badge.flag { background: #ffe0b2; color: #e65100; }
.move-badge.scout { background: #e3f2fd; color: #1565c0; }
.move-badge.assess { background: #f3e5f5; color: #7b1fa2; }
.move-badge.prioritization { background: #fff3e0; color: #e65100; }
.move-badge.ingest { background: #e8f5e9; color: #2e7d32; }
.move-summary { font-size: 0.78rem; color: #555; }
.move-fields {
  margin: 0.2rem 0 0.2rem 1rem; font-size: 0.76rem;
}
.move-field { display: flex; gap: 0.3rem; padding: 0.05rem 0; }
.field-key { color: #888; font-weight: 500; white-space: nowrap; }
.field-val { color: #333; word-break: break-word; }

/* Dispatch nav */
.dispatch-nav {
  margin: 0.4rem 0; font-size: 0.82rem;
  padding: 0.4rem 0.6rem; background: rgba(0,0,0,.02);
  border-radius: 5px; border: 1px solid rgba(0,0,0,.06);
}
.dispatch-nav ol { margin-left: 1.4rem; }
.dispatch-nav li { margin: 0.15rem 0; }
.dispatch-link { text-decoration: none; }
.dispatch-link:hover .move-badge { filter: brightness(0.9); }
.dispatch-budget { font-size: 0.72rem; color: #888; }
.dispatch-reason { font-size: 0.78rem; color: #666; }
.dispatch-plan { margin: 0.2rem 0; }
.dispatch-item { margin: 0.1rem 0; display: flex; align-items: baseline; gap: 0.3rem; }
.dispatch-index { font-size: 0.72rem; color: #aaa; }
.skipped { opacity: 0.5; }

/* Review */
.review {
  font-size: 0.8rem; margin: 0.4rem 0; padding: 0.3rem 0.5rem;
  background: rgba(0,0,0,.03); border-radius: 4px;
}
.review-section { font-size: 0.8rem; color: #555; }

.children { margin-top: 0.3rem; }

.footer { font-size: 0.72rem; color: #ccc; margin-top: 2rem; text-align: center; }
"""


def generate_trace(question_id_or_call_id: str, db: DB) -> Path:
    """Generate an HTML trace visualization. Returns the file path."""
    TRACES_DIR.mkdir(parents=True, exist_ok=True)

    page = db.get_page(question_id_or_call_id)
    if page:
        question_label = page.summary[:60]
        root_calls = db.get_root_calls_for_question(question_id_or_call_id)
        if not root_calls:
            raise ValueError(f"No calls found for question {question_id_or_call_id}")
        body_html = ""
        for rc in root_calls:
            body_html += _render_call_node(rc, db, depth=0)
    else:
        call = db.get_call(question_id_or_call_id)
        if not call:
            raise ValueError(
                f"No question or call found for ID: {question_id_or_call_id}"
            )
        question_label = f"Call {call.id[:8]} ({call.call_type.value})"
        body_html = _render_call_node(call, db, depth=0)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = "".join(c if c.isalnum() or c in " -" else "" for c in question_label[:50])
    slug = slug.strip().replace(" ", "-").lower()
    output_path = TRACES_DIR / f"{timestamp}-{slug}.html"

    html = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="UTF-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"  <title>Execution Trace: {escape(question_label)}</title>\n"
        f"  <style>{_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        '  <p class="subtitle">Execution Trace</p>\n'
        f"  <h1>{escape(question_label)}</h1>\n"
        f"  {body_html}\n"
        f'  <p class="footer">Generated {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")} UTC</p>\n'
        "</body>\n"
        "</html>"
    )

    output_path.write_text(html, encoding="utf-8")
    return output_path
