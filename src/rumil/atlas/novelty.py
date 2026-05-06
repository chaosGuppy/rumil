"""Detect novelty in real run/call/exchange data — things atlas's
static registries don't know about.

The loop atlas needs to close: not just "what does the system have?"
but "what does the system DO that I'm not modeling yet?". Four
detectors:

1. ``unknown_tool_use`` — tool_calls in recent exchanges with
   ``name`` not in any registered ``DispatchDef.name`` /
   ``MoveDef.name``. Catches new dispatch tools or moves emitted by
   the LLM that atlas's registry doesn't list.
2. ``unknown_trace_event`` — event-type strings in trace_json that
   aren't in ``event_keys.ATLAS_READS`` and don't appear on the
   ``TraceEvent`` discriminated union. Means atlas isn't reading
   some new event yet.
3. ``unknown_call_type`` — calls.call_type values that aren't on the
   ``CallType`` enum. Should be impossible given the FK, but cheap
   to verify.
4. ``orphan_rendered_prompt`` — system_prompt prefixes that don't
   match any prompts/*.md file. Heuristic; catches bypass paths
   that build a system_prompt from scratch.

All scans are bounded by an ``n_scanned`` argument and return one
``NoveltyItem`` per distinct unknown target with a sample id.
"""

from __future__ import annotations

import logging

from rumil.atlas.schemas import NoveltyItem, NoveltyReport
from rumil.calls.dispatches import (
    DISPATCH_DEFS,
    RECURSE_CLAIM_DISPATCH_DEF,
    RECURSE_DISPATCH_DEF,
)
from rumil.database import DB
from rumil.models import CallType
from rumil.moves.registry import MOVES
from rumil.prompts import PROMPTS_DIR

log = logging.getLogger(__name__)


def _known_tool_names() -> set[str]:
    names: set[str] = set()
    for d in DISPATCH_DEFS.values():
        names.add(d.name)
    names.add(RECURSE_DISPATCH_DEF.name)
    names.add(RECURSE_CLAIM_DISPATCH_DEF.name)
    for m in MOVES.values():
        names.add(m.name)
    return names


def _known_event_keys() -> set[str]:
    """Every event Literal that appears in TraceEvent. Atlas may not
    read all of them yet, but anything outside this union is a strict
    novelty signal.

    ``TraceEvent`` is ``Annotated[Union[...], Field(discriminator=...)]``,
    so we have to unwrap two layers: ``get_args(TraceEvent)`` gives
    ``(Union[...], FieldInfo)``; ``get_args`` on the union gives the
    member classes.
    """
    import typing

    from rumil.tracing.trace_events import TraceEvent  # discriminated union

    names: set[str] = set()
    annotated_args = typing.get_args(TraceEvent)
    if not annotated_args:
        return names
    union_member = annotated_args[0]
    members = typing.get_args(union_member) or (union_member,)
    for member in members:
        try:
            default = member.model_fields["event"].default
            if isinstance(default, str):
                names.add(default)
        except Exception:
            continue
    return names


def _prompt_prefix_index(prefix_chars: int = 200) -> dict[str, str]:
    """Map prompt-file first-N-chars-stripped → file name. Used to
    detect rendered prompts whose system_prompt doesn't match any file
    we know about (heuristic for build_system_prompt bypass paths).
    """
    out: dict[str, str] = {}
    for path in PROMPTS_DIR.glob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        prefix = text.strip()[:prefix_chars]
        if prefix:
            out[prefix] = path.name
    return out


async def build_novelty_report(
    db: DB,
    project_id: str | None = None,
    scan_exchanges: int = 500,
    scan_calls: int = 500,
) -> NoveltyReport:
    items: list[NoveltyItem] = []
    counts: dict[str, int] = {}

    def _record(item: NoveltyItem) -> None:
        items.append(item)
        counts[item.kind] = counts.get(item.kind, 0) + 1

    known_tools = _known_tool_names()
    known_events = _known_event_keys()
    known_call_types = {ct.value for ct in CallType}
    prompt_prefixes = _prompt_prefix_index()

    # --- Scan recent exchanges for unknown tool_calls + orphan prompts ---
    ex_query = (
        db.client.table("call_llm_exchanges")
        .select("id, call_id, tool_calls, system_prompt")
        .order("created_at", desc=True)
        .limit(scan_exchanges)
    )
    ex_res = await db._execute(ex_query)
    ex_rows = list(ex_res.data or [])
    seen_unknown_tools: dict[str, NoveltyItem] = {}
    seen_orphan_prompts: dict[str, NoveltyItem] = {}

    if ex_rows:
        if project_id:
            call_ids = list({str(r.get("call_id")) for r in ex_rows if r.get("call_id")})
            cres = await db._execute(
                db.client.table("calls").select("id, project_id").in_("id", call_ids)
            )
            project_by_call = {
                str(c.get("id")): str(c.get("project_id") or "") for c in (cres.data or [])
            }
        else:
            project_by_call = {}

        for r in ex_rows:
            cid = str(r.get("call_id") or "")
            if project_id and project_by_call.get(cid) != project_id:
                continue
            tcs = r.get("tool_calls") or []
            if isinstance(tcs, list):
                for tc in tcs:
                    if not isinstance(tc, dict):
                        continue
                    name = str(tc.get("name") or "")
                    if not name or name in known_tools:
                        continue
                    if name not in seen_unknown_tools:
                        seen_unknown_tools[name] = NoveltyItem(
                            kind="unknown_tool_use",
                            target=name,
                            detail=(
                                f"tool_use ``{name}`` appears in call_llm_exchanges "
                                "but matches no registered DispatchDef or MoveDef."
                            ),
                            sample_call_id=cid or None,
                            seen_count=0,
                        )
                    seen_unknown_tools[name].seen_count += 1
            sys_prompt = str(r.get("system_prompt") or "")
            if sys_prompt:
                prefix = sys_prompt.strip()[:200]
                if prefix and prefix not in prompt_prefixes:
                    # only record once per (truncated) prefix
                    if prefix not in seen_orphan_prompts:
                        seen_orphan_prompts[prefix] = NoveltyItem(
                            kind="orphan_rendered_prompt",
                            target=prefix[:80] + ("…" if len(prefix) > 80 else ""),
                            detail=(
                                "system_prompt prefix doesn't match any prompts/*.md "
                                "file — likely assembled outside build_system_prompt "
                                "(versus_judge / generate_artefact / chat / clean / "
                                "evaluate paths are known bypasses)."
                            ),
                            sample_call_id=cid or None,
                            seen_count=0,
                        )
                    seen_orphan_prompts[prefix].seen_count += 1

    for it in seen_unknown_tools.values():
        _record(it)
    for it in seen_orphan_prompts.values():
        _record(it)

    # --- Scan recent calls for unknown call_types + unknown event keys ---
    call_query = (
        db.client.table("calls")
        .select("id, run_id, call_type, trace_json")
        .order("created_at", desc=True)
        .limit(scan_calls)
    )
    if project_id:
        call_query = call_query.eq("project_id", project_id)
    cres = await db._execute(call_query)
    call_rows = list(cres.data or [])
    seen_unknown_ct: dict[str, NoveltyItem] = {}
    seen_unknown_event: dict[str, NoveltyItem] = {}

    for c in call_rows:
        ct = str(c.get("call_type") or "")
        if ct and ct not in known_call_types:
            if ct not in seen_unknown_ct:
                seen_unknown_ct[ct] = NoveltyItem(
                    kind="unknown_call_type",
                    target=ct,
                    detail=(
                        f"calls.call_type=``{ct}`` is not a CallType enum value. "
                        "FK should prevent this — investigate if seen."
                    ),
                    sample_call_id=str(c.get("id") or "") or None,
                    sample_run_id=str(c.get("run_id") or "") or None,
                    seen_count=0,
                )
            seen_unknown_ct[ct].seen_count += 1

        events = c.get("trace_json") or []
        if isinstance(events, list):
            for e in events:
                if not isinstance(e, dict):
                    continue
                etype = str(e.get("event") or "")
                if not etype or etype in known_events:
                    continue
                if etype not in seen_unknown_event:
                    seen_unknown_event[etype] = NoveltyItem(
                        kind="unknown_trace_event",
                        target=etype,
                        detail=(
                            f"trace event ``{etype}`` appears in trace_json but is "
                            "not on the TraceEvent discriminated union — atlas isn't "
                            "reading it. Add to trace_events.py if intentional, or "
                            "remove the emission."
                        ),
                        sample_call_id=str(c.get("id") or "") or None,
                        sample_run_id=str(c.get("run_id") or "") or None,
                        seen_count=0,
                    )
                seen_unknown_event[etype].seen_count += 1

    for it in seen_unknown_ct.values():
        _record(it)
    for it in seen_unknown_event.values():
        _record(it)

    return NoveltyReport(
        items=items,
        counts_by_kind=counts,
        n_scanned_exchanges=len(ex_rows),
        n_scanned_calls=len(call_rows),
    )
