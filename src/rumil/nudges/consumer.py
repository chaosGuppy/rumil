"""Nudge consumption logic — read side of mid-run steering.

Three responsibilities:

1. ``filter_dispatch_sequences`` — given the prioritization output and the
   currently-active nudges on a run, drop dispatch items that a ``hard``
   ``constrain_dispatch`` or ``veto_call`` nudge prohibits. Ban-unions
   across matching nudges so anything blocked by any nudge is blocked.
2. ``render_steering_context`` — concatenate soft-text from matching
   nudges newest-first into a "Human steering" block for LLM context.
3. ``consume_one_shot`` — flip ``one_shot`` nudges to ``consumed`` after a
   batch / call has used them. Persistent nudges stay active.

A small ``build_applied_event`` helper packages the fired nudges into a
``NudgeAppliedEvent`` for the trace.
"""

from collections.abc import Sequence

from rumil.database import DB
from rumil.models import Dispatch, NudgeDurability, NudgeKind, RunNudge
from rumil.tracing.trace_events import NudgeAppliedEvent, NudgeSummaryItem


def filter_dispatch_sequences(
    sequences: Sequence[Sequence[Dispatch]],
    nudges: Sequence[RunNudge],
) -> tuple[list[list[Dispatch]], list[RunNudge], int]:
    """Return ``(filtered_sequences, fired_nudges, dropped_count)``.

    A nudge *fires* if it removed at least one dispatch. Soft and
    non-matching nudges are not returned here (they only surface via
    ``render_steering_context``).
    """
    constrain_nudges: list[RunNudge] = [
        n
        for n in nudges
        if n.hard and n.kind == NudgeKind.CONSTRAIN_DISPATCH and n.scope.call_types
    ]

    if not constrain_nudges:
        return [list(seq) for seq in sequences], [], 0

    fired: list[RunNudge] = []
    fired_ids: set[str] = set()
    dropped = 0
    filtered: list[list[Dispatch]] = []

    for seq in sequences:
        kept: list[Dispatch] = []
        for dispatch in seq:
            call_type = dispatch.call_type.value
            dispatch_qid = getattr(dispatch.payload, "question_id", "") or ""
            dropped_here = False
            for n in constrain_nudges:
                assert n.scope.call_types is not None
                if call_type not in n.scope.call_types:
                    continue
                if n.scope.question_ids and dispatch_qid not in n.scope.question_ids:
                    continue
                if n.id not in fired_ids:
                    fired.append(n)
                    fired_ids.add(n.id)
                dropped_here = True
            if dropped_here:
                dropped += 1
                continue
            kept.append(dispatch)
        if kept:
            filtered.append(kept)

    return filtered, fired, dropped


def render_steering_context(
    nudges: Sequence[RunNudge],
) -> str:
    """Return a formatted "Human steering" block, newest-first.

    Returns "" when there are no nudges to render — caller should not
    emit the section header in that case.
    """
    soft_lines: list[str] = []
    for n in sorted(nudges, key=lambda x: x.created_at, reverse=True):
        if not n.soft_text:
            continue
        label_bits: list[str] = [n.kind.value]
        if n.hard:
            label_bits.append("hard")
        if n.durability == NudgeDurability.PERSISTENT:
            label_bits.append("persistent")
        else:
            label_bits.append("one-shot")
        label = f"[{', '.join(label_bits)}]"
        text_parts: list[str] = [n.soft_text.strip()]
        if n.scope.call_types:
            text_parts.append(f"scoped to call types: {', '.join(n.scope.call_types)}")
        if n.scope.question_ids:
            text_parts.append(f"scoped to questions: {', '.join(n.scope.question_ids)}")
        soft_lines.append(f"- {label} {' — '.join(text_parts)}")

    if not soft_lines:
        return ""

    header = (
        "## Human steering\n\n"
        "A human operator has left the following steering notes for this run. "
        "Hard items are enforced by the system; treat soft notes as strong "
        "guidance unless they would require unsafe or nonsensical actions.\n\n"
    )
    return header + "\n".join(soft_lines)


def build_applied_event(
    *,
    phase: str,
    fired_hard: Sequence[RunNudge],
    fired_soft: Sequence[RunNudge],
    dropped_count: int,
) -> NudgeAppliedEvent:
    items: list[NudgeSummaryItem] = []
    for n in fired_hard:
        items.append(
            NudgeSummaryItem(
                nudge_id=n.id,
                kind=n.kind.value,
                author_kind=n.author_kind.value,
                hard=True,
                soft_text=n.soft_text,
                effect="hard_filter",
            )
        )
    for n in fired_soft:
        items.append(
            NudgeSummaryItem(
                nudge_id=n.id,
                kind=n.kind.value,
                author_kind=n.author_kind.value,
                hard=False,
                soft_text=n.soft_text,
                effect="context_injection",
            )
        )
    return NudgeAppliedEvent(
        phase=phase,
        applied=items,
        filtered_dispatch_count=dropped_count,
    )


async def consume_one_shot(db: DB, nudges: Sequence[RunNudge]) -> None:
    for n in nudges:
        if n.durability == NudgeDurability.ONE_SHOT:
            await db.nudges.mark_consumed(n.id)


async def apply_soft_nudges_to_context(
    db: DB,
    *,
    call_type: str,
    question_id: str | None,
    context_text: str,
) -> tuple[str, list[RunNudge]]:
    """Prepend a "Human steering" block to ``context_text`` when matching
    soft nudges are active for this call.

    Returns ``(new_context_text, applied_nudges)``. ``applied_nudges`` is
    the list of nudges that fired — the caller is responsible for emitting
    a ``NudgeAppliedEvent`` and calling ``consume_one_shot``. Hard nudges
    are intentionally ignored here (the orchestrator applies them).
    """
    question_ids: list[str] | None = [question_id] if question_id else None
    nudges = await db.nudges.get_active_for_run(
        db.run_id,
        call_type=call_type,
        question_ids=question_ids,
    )
    applied = [n for n in nudges if not n.hard and n.soft_text]
    if not applied:
        return context_text, []
    steering = render_steering_context(applied)
    if not steering:
        return context_text, []
    return steering + "\n\n" + context_text, applied
