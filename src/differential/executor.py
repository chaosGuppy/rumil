"""
Execute parsed moves against the workspace database and file system.
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from differential.database import DB
from differential.models import (
    Call, ConsiderationDirection, LinkType,
    Page, PageLayer, PageLink, PageType, Workspace,
)
from differential.parser import Move

PAGES_DIR = Path(__file__).parent.parent.parent / "pages"


def _pages_dir(workspace: Workspace) -> Path:
    d = PAGES_DIR / workspace.value
    d.mkdir(parents=True, exist_ok=True)
    return d


def _page_filename(page: Page) -> str:
    slug = page.summary[:60].lower()
    slug = "".join(c if c.isalnum() or c in " -" else "" for c in slug)
    slug = slug.strip().replace(" ", "-")
    short_id = page.id[:8]
    return f"{page.page_type.value}-{short_id}-{slug}.md"


def _write_page_file(page: Page) -> None:
    """Write a human-readable markdown file for a page."""
    d = _pages_dir(page.workspace)
    filepath = d / _page_filename(page)

    extra = json.loads(page.extra) if page.extra else {}

    lines = [
        f"# {page.summary}",
        "",
        f"**Type:** {page.page_type.value}  ",
        f"**Layer:** {page.layer.value}  ",
        f"**ID:** `{page.id}`  ",
        f"**Created:** {page.created_at.strftime('%Y-%m-%d %H:%M:%S')} UTC  ",
        f"**Epistemic status:** {page.epistemic_status:.2f} — {page.epistemic_type}  ",
        f"**Provenance:** {page.provenance_call_type} call `{page.provenance_call_id[:8]}`  ",
    ]

    if page.is_superseded:
        lines.append(f"**SUPERSEDED by:** `{page.superseded_by}`  ")

    if extra:
        lines.append("")
        lines.append("## Metadata")
        for k, v in extra.items():
            lines.append(f"- **{k}:** {v}")

    lines += ["", "---", "", page.content]

    filepath.write_text("\n".join(lines), encoding="utf-8")


def _resolve_workspace(payload: dict) -> Workspace:
    ws = payload.get("workspace", "research").lower()
    return Workspace.RESEARCH if ws == "research" else Workspace.PRIORITIZATION


def execute_move(move: Move, call: Call, db: DB, last_created_id: Optional[str] = None) -> Optional[str]:
    """
    Execute a single move. Returns the ID of any page created, or None.
    last_created_id: ID of the most recently created page, for LAST_CREATED resolution.
    """
    p = move.payload
    mt = move.move_type

    # Resolve LAST_CREATED placeholder in any string field
    if last_created_id:
        p = {
            k: (last_created_id if v == "LAST_CREATED" else v)
            for k, v in p.items()
        }

    if mt == "CREATE_CLAIM":
        return _create_page(p, call, db, PageType.CLAIM, PageLayer.SQUIDGY)

    elif mt == "CREATE_QUESTION":
        return _create_page(p, call, db, PageType.QUESTION, PageLayer.SQUIDGY)

    elif mt == "CREATE_JUDGEMENT":
        return _create_page(p, call, db, PageType.JUDGEMENT, PageLayer.SQUIDGY)

    elif mt == "CREATE_CONCEPT":
        return _create_page(p, call, db, PageType.CONCEPT, PageLayer.SQUIDGY)

    elif mt == "CREATE_WIKI_PAGE":
        return _create_page(p, call, db, PageType.WIKI, PageLayer.WIKI)

    elif mt == "LINK_CONSIDERATION":
        _link_consideration(p, db)

    elif mt == "LINK_CHILD_QUESTION":
        _link_pages(p, db, LinkType.CHILD_QUESTION)

    elif mt == "LINK_RELATED":
        _link_pages(p, db, LinkType.RELATED)

    elif mt == "SUPERSEDE_PAGE":
        _supersede(p, call, db)

    elif mt == "FLAG_FUNNINESS":
        note = p.get("note", "")
        page_id = db.resolve_page_id(p.get("page_id", ""))
        db.save_page_flag("funniness", call_id=call.id, note=note, page_id=page_id)
        print(f"  [flag] Funniness flagged: {note}")

    elif mt == "REPORT_DUPLICATE":
        pid_a = db.resolve_page_id(p.get("page_id_a", ""))
        pid_b = db.resolve_page_id(p.get("page_id_b", ""))
        db.save_page_flag("duplicate", call_id=call.id, page_id_a=pid_a, page_id_b=pid_b)
        print(f"  [flag] Duplicate reported: {p.get('page_id_a')} <-> {p.get('page_id_b')}")

    elif mt == "PROPOSE_HYPOTHESIS":
        return _propose_hypothesis(p, call, db)

    elif mt == "LOAD_PAGE":
        pass  # pre-phase move; handled before executor runs, safe to ignore here

    else:
        print(f"  [executor] Unknown move type: {mt}")

    return None


def _create_page(
    payload: dict,
    call: Call,
    db: DB,
    page_type: PageType,
    layer: PageLayer,
) -> str:
    workspace = _resolve_workspace(payload)
    extra: dict[str, Any] = {}

    # Pull out well-known extra fields by page type
    for key in ["status", "remaining_fruit", "parent_question_id",
                 "key_dependencies", "sensitivity_analysis", "confidence_type",
                 "decomposition_status", "source_url", "source_id",
                 "direction", "strength", "hypothesis"]:
        if key in payload:
            extra[key] = payload[key]

    page = Page(
        page_type=page_type,
        layer=layer,
        workspace=workspace,
        content=payload.get("content", ""),
        summary=payload.get("summary", ""),
        epistemic_status=float(payload.get("epistemic_status", 2.5)),
        epistemic_type=payload.get("epistemic_type", ""),
        provenance_model=payload.get("provenance_model", "claude-opus-4-6"),
        provenance_call_type=call.call_type.value,
        provenance_call_id=call.id,
        extra=json.dumps(extra),
    )

    db.save_page(page)
    _write_page_file(page)
    print(f"  [+] {page_type.value}: {page.summary[:70]} [{page.id[:8]}]")
    return page.id


def _link_consideration(payload: dict, db: DB) -> None:
    claim_id = payload.get("claim_id") or payload.get("from_page_id")
    question_id = payload.get("question_id") or payload.get("to_page_id")
    if not claim_id or not question_id:
        print(f"  [executor] LINK_CONSIDERATION missing claim_id or question_id: {payload}")
        return

    claim_id = db.resolve_page_id(claim_id)
    question_id = db.resolve_page_id(question_id)
    if not claim_id or not question_id:
        print(f"  [executor] LINK_CONSIDERATION skipped — one or both page IDs not found: {payload}")
        return

    direction_str = payload.get("direction", "neutral").lower()
    try:
        direction = ConsiderationDirection(direction_str)
    except ValueError:
        direction = ConsiderationDirection.NEUTRAL

    link = PageLink(
        from_page_id=claim_id,
        to_page_id=question_id,
        link_type=LinkType.CONSIDERATION,
        direction=direction,
        strength=float(payload.get("strength", 2.5)),
        reasoning=payload.get("reasoning", ""),
    )
    db.save_link(link)
    print(f"  [~] Consideration: {db.page_label(claim_id)} -> {db.page_label(question_id)} ({direction_str})")


def _link_pages(payload: dict, db: DB, link_type: LinkType) -> None:
    from_id = payload.get("from_page_id") or payload.get("parent_id")
    to_id = payload.get("to_page_id") or payload.get("child_id")
    if not from_id or not to_id:
        print(f"  [executor] Link missing from/to page IDs: {payload}")
        return

    from_id = db.resolve_page_id(from_id)
    to_id = db.resolve_page_id(to_id)
    if not from_id or not to_id:
        print(f"  [executor] {link_type.value} link skipped — one or both page IDs not found: {payload}")
        return

    link = PageLink(
        from_page_id=from_id,
        to_page_id=to_id,
        link_type=link_type,
        reasoning=payload.get("reasoning", ""),
    )
    db.save_link(link)
    print(f"  [~] {link_type.value}: {db.page_label(from_id)} -> {db.page_label(to_id)}")


def _supersede(payload: dict, call: Call, db: DB) -> None:
    old_id = payload.get("old_page_id")
    if not old_id:
        print(f"  [executor] SUPERSEDE_PAGE missing old_page_id: {payload}")
        return

    old_page = db.get_page(old_id)
    if not old_page:
        print(f"  [executor] SUPERSEDE_PAGE: page {old_id} not found")
        return

    # Create the new page first
    new_id = _create_page(payload, call, db, old_page.page_type, old_page.layer)
    db.supersede_page(old_id, new_id)
    print(f"  [~] Superseded {db.page_label(old_id)} -> {db.page_label(new_id)}")


def _propose_hypothesis(payload: dict, call: Call, db: DB) -> Optional[str]:
    parent_id = db.resolve_page_id(payload.get("parent_question_id", ""))
    if not parent_id:
        print(f"  [executor] PROPOSE_HYPOTHESIS: parent_question_id not found: {payload.get('parent_question_id')}")
        return None

    hypothesis_text = payload.get("hypothesis", "").strip()
    if not hypothesis_text:
        print("  [executor] PROPOSE_HYPOTHESIS: missing hypothesis text")
        return None

    reasoning = payload.get("reasoning", "")
    epistemic_status = float(payload.get("epistemic_status", 2.5))
    direction_str = payload.get("direction", "neutral").lower()
    strength = float(payload.get("strength", 2.5))

    # 1. Create the claim (hypothesis in assertive form, visible as consideration on parent)
    claim_content = hypothesis_text
    if reasoning:
        claim_content += f"\n\n{reasoning}"

    claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=claim_content,
        summary=hypothesis_text[:120],
        epistemic_status=epistemic_status,
        epistemic_type="hypothesis",
        provenance_model=payload.get("provenance_model", "claude-opus-4-6"),
        provenance_call_type=call.call_type.value,
        provenance_call_id=call.id,
        extra=json.dumps({"hypothesis": True}),
    )
    db.save_page(claim)
    _write_page_file(claim)
    print(f"  [+] hypothesis claim: {db.page_label(claim.id)}")

    try:
        direction = ConsiderationDirection(direction_str)
    except ValueError:
        direction = ConsiderationDirection.NEUTRAL

    db.save_link(PageLink(
        from_page_id=claim.id,
        to_page_id=parent_id,
        link_type=LinkType.CONSIDERATION,
        direction=direction,
        strength=strength,
        reasoning=reasoning,
    ))
    print(f"  [~] Consideration: {db.page_label(claim.id)} -> {db.page_label(parent_id)} ({direction_str})")

    # 2. Create the hypothesis question (investigation vehicle)
    q_text = f"What should we make of the hypothesis that {hypothesis_text}?"
    question = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=q_text,
        summary=q_text[:120],
        epistemic_status=2.5,
        epistemic_type="open question",
        provenance_model=payload.get("provenance_model", "claude-opus-4-6"),
        provenance_call_type=call.call_type.value,
        provenance_call_id=call.id,
        extra=json.dumps({"hypothesis": True, "status": "open"}),
    )
    db.save_page(question)
    _write_page_file(question)
    print(f"  [+] hypothesis question: {db.page_label(question.id)}")

    db.save_link(PageLink(
        from_page_id=parent_id,
        to_page_id=question.id,
        link_type=LinkType.CHILD_QUESTION,
        reasoning=f"Hypothesis: {hypothesis_text[:80]}",
    ))
    print(f"  [~] child_question: {db.page_label(parent_id)} -> {db.page_label(question.id)}")

    return question.id


def execute_all_moves(parsed_output, call: Call, db: DB) -> list[str]:
    """Execute all moves in a parsed output. Returns list of created page IDs."""
    created_ids = []
    last_created_id = None
    for move in parsed_output.moves:
        page_id = execute_move(move, call, db, last_created_id=last_created_id)
        if page_id:
            created_ids.append(page_id)
            last_created_id = page_id
    return created_ids
