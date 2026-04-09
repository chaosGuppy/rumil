"""Shared models and execution logic for workspace update pipelines."""

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from rumil.calls.call_registry import ASSESS_CALL_CLASSES
from rumil.calls.common import (
    ABSTRACT_INSTRUCTION,
    PageSummaryItem,
    save_page_abstracts,
)
from rumil.context import build_embedding_based_context
from rumil.moves.base import HEADLINE_DESCRIPTION
from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, structured_call
from rumil.models import (
    Call,
    CallType,
    LinkRole,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
)
from rumil.moves.base import (
    _copy_consideration_links,
    extract_and_link_citations,
    link_pages,
    write_page_file,
)
from rumil.moves.link_consideration import (
    LinkConsiderationPayload,
    execute as execute_link_consideration,
)
from rumil.moves.remove_link import (
    RemoveLinkPayload,
    execute as execute_remove_link,
)
from rumil.settings import get_settings
from rumil.tracing.trace_events import ClaimReassessedEvent, ReassessTriggeredEvent
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
_UPDATE_SEMAPHORE = asyncio.Semaphore(15)


class UpdateOperation(BaseModel):
    page_id: str = Field(
        default="",
        description="8-char short ID of the page to update (for reassess_claim / reassess_question)",
    )
    operation: str = Field(
        description=(
            "Type of update: 'reassess_claim', 'reassess_claims', or 'reassess_question'"
        )
    )
    findings_summary: str = Field(
        default="",
        description=(
            "For reassess_claim: concise summary of the web research "
            "findings that bear on this claim, including relevant URLs. "
            "For reassess_question / reassess_claims: leave empty."
        ),
    )
    page_ids: list[str] = Field(
        default_factory=list,
        description=(
            "For reassess_claims: list of 8-char short IDs of claims to reassess together."
        ),
    )
    in_light_of: list[str] = Field(
        default_factory=list,
        description=(
            "For reassess_claims / reassess_question: page IDs whose content "
            "should inform the reassessment. The update plan defines a chain "
            "of updates — each operation's in_light_of should list the "
            "updated pages that directly influence it. If page A depends on "
            "page B only via an intermediate page C, B should not appear in "
            "A's in_light_of (C is sufficient). "
            "If a page ID points to a question with an active judgement, "
            "the judgement is used instead."
        ),
    )
    guidance: str = Field(
        default="",
        description=(
            "For reassess_claims: free-text instruction explaining what "
            "the reassessment should achieve (e.g. reconcile conflicting claims)."
        ),
    )


class UpdatePlan(BaseModel):
    waves: list[list[UpdateOperation]] = Field(
        description=(
            "Ordered list of waves. Each wave is a list of update "
            "operations that execute concurrently. Waves execute in "
            "sequence — all operations in wave N complete before "
            "wave N+1 starts."
        )
    )


class ReassessedClaim(BaseModel):
    headline: str = Field(description=HEADLINE_DESCRIPTION)
    content: str = Field(description="Full standalone content of the replacement claim")
    credence: int = Field(description="Probability bucket 1-9 (1=very unlikely, 9=very likely)")
    robustness: int = Field(description="Resilience of view 1-5 (1=fragile, 5=very robust)")


class _PageAbstractList(BaseModel):
    summaries: list[PageSummaryItem]


def save_checkpoint(call: Call, key: str, data: Any) -> None:
    """Persist a stage checkpoint into ``call.call_params``."""
    if call.call_params is None:
        call.call_params = {}
    call.call_params.setdefault("checkpoints", {})[key] = data


def normalize_plan(raw: Any) -> dict:
    """Normalize the agent's free-form plan JSON into our UpdatePlan schema.

    The model may produce waves as objects with an ``operations`` key and
    use ``type`` instead of ``operation`` on each item.
    """
    if not isinstance(raw, dict):
        return {"waves": []}

    raw_waves = raw.get("waves", [])
    normalized_waves: list[list[dict]] = []

    for wave in raw_waves:
        if isinstance(wave, list):
            ops = wave
        elif isinstance(wave, dict):
            ops = wave.get("operations", [])
        else:
            continue

        normalized_ops: list[dict] = []
        for op in ops:
            if not isinstance(op, dict):
                continue
            normalized_op: dict = {
                "page_id": op.get("page_id", ""),
                "operation": op.get("operation") or op.get("type", ""),
                "findings_summary": op.get("findings_summary")
                or op.get("findings", ""),
                "page_ids": op.get("page_ids", []),
                "in_light_of": op.get("in_light_of", []),
                "guidance": op.get("guidance", ""),
            }
            normalized_ops.append(normalized_op)

        if normalized_ops:
            normalized_waves.append(normalized_ops)

    return {"waves": normalized_waves}


def log_plan(plan: UpdatePlan) -> None:
    """Log the update plan for visibility."""
    lines = ["Update plan:"]
    for i, wave in enumerate(plan.waves, 1):
        op_strs: list[str] = []
        for op in wave:
            if op.operation == "reassess_claims":
                ids = ",".join(pid[:8] for pid in op.page_ids)
                op_strs.append(f"[{ids}](reassess_claims)")
            else:
                op_strs.append(f"{op.page_id[:8]}({op.operation})")
        lines.append(f"  Wave {i}: {', '.join(op_strs)}")
    log.info("\n".join(lines))


async def execute_update_plan(
    plan: UpdatePlan,
    call: Call,
    db: DB,
    trace: CallTrace,
) -> None:
    """Execute the update plan wave by wave."""
    for i, wave in enumerate(plan.waves, 1):
        log.info("Executing wave %d (%d operations)", i, len(wave))

        async def _execute_op(op: UpdateOperation) -> None:
            async with _UPDATE_SEMAPHORE:
                if op.operation == "reassess_claim":
                    await reassess_claim(
                        op.page_id, op.findings_summary, call, db, trace
                    )
                elif op.operation == "reassess_claims":
                    await reassess_claims(
                        op.page_ids, op.in_light_of, op.guidance, call, db, trace
                    )
                elif op.operation == "reassess_question":
                    await reassess_question(
                        op.page_id, op.in_light_of, call, db, trace
                    )
                else:
                    log.warning("Unknown operation type: %s", op.operation)

        await asyncio.gather(*[_execute_op(op) for op in wave])


async def reassess_claim(
    page_id: str,
    findings: str,
    call: Call,
    db: DB,
    trace: CallTrace,
) -> None:
    """Reassess a claim using embedding context + linked pages + findings."""
    resolved_id = await db.resolve_page_id(page_id)
    if not resolved_id:
        log.warning("Could not resolve claim page ID: %s", page_id)
        return
    old_page = await db.get_page(resolved_id)
    if not old_page or not old_page.is_active():
        log.warning("Claim page %s not found or inactive", page_id)
        return
    system_prompt = (
        (_PROMPTS_DIR / "preamble.md").read_text()
        + "\n\n"
        + (_PROMPTS_DIR / "grounding-reassess-claim.md").read_text()
        + "\n\n"
        + (_PROMPTS_DIR / "citations.md").read_text()
    )

    ctx_result = await build_embedding_based_context(
        old_page.headline,
        db,
    )

    links_from = await db.get_links_from(old_page.id)
    links_to = await db.get_links_to(old_page.id)
    linked_ids = (
        {link.to_page_id for link in links_from}
        | {link.from_page_id for link in links_to}
    )
    linked_pages = await db.get_pages_by_ids(list(linked_ids))
    linked_text_parts: list[str] = []
    for pid, lp in linked_pages.items():
        if lp.is_active():
            linked_text_parts.append(
                f"### `{pid[:8]}` — {lp.headline} ({lp.page_type.value})\n\n"
                f"{lp.content}"
            )
    linked_text = "\n\n---\n\n".join(linked_text_parts) if linked_text_parts else ""

    user_parts: list[str] = [
        f"## Workspace context\n\n{ctx_result.context_text}",
    ]
    if linked_text:
        user_parts.append(f"## Linked pages\n\n{linked_text}")
    user_parts.append(
        "## Claim to reassess\n\n"
        f"**Headline:** {old_page.headline}\n"
        f"**ID:** `{old_page.id[:8]}`\n\n"
        f"{old_page.content}"
    )
    if findings:
        user_parts.append(
            "## Web research findings\n\n"
            "The following findings directly bear on this claim:\n\n"
            f"{findings}"
        )

    user_message = "\n\n".join(user_parts)

    meta = LLMExchangeMetadata(
        call_id=call.id,
        phase=f"reassess_claim_{old_page.id[:8]}",
    )
    result = await structured_call(
        system_prompt,
        user_message=user_message,
        response_model=ReassessedClaim,
        metadata=meta,
        db=db,
    )

    if result.parsed is None:
        log.warning("Reassess claim %s returned no data", old_page.id[:8])
        return

    reassessed = result.parsed

    new_page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=old_page.workspace,
        content=reassessed.content,
        headline=reassessed.headline,
        credence=reassessed.credence,
        robustness=reassessed.robustness,
        provenance_model="claude-opus-4-6",
        provenance_call_type=call.call_type.value,
        provenance_call_id=call.id,
        project_id=old_page.project_id,
    )
    await db.save_page(new_page)
    write_page_file(new_page)
    await extract_and_link_citations(new_page.id, new_page.content, db)

    await db.supersede_page(old_page.id, new_page.id)
    await _copy_consideration_links(old_page.id, new_page.id, db)

    log.info(
        "Reassessed claim %s -> %s: %s",
        old_page.id[:8],
        new_page.id[:8],
        reassessed.headline[:70],
    )

    await trace.record(
        ClaimReassessedEvent(
            old_page_id=old_page.id,
            new_page_id=new_page.id,
            headline=reassessed.headline,
        )
    )


async def reassess_question(
    page_id: str,
    in_light_of: Sequence[str],
    call: Call,
    db: DB,
    trace: CallTrace,
    assess_variant: str | None = None,
) -> None:
    """Reassess a question's judgement by dispatching an AssessCall.

    *in_light_of* page IDs are resolved (questions → latest judgement) and
    passed as ``context_page_ids`` on the child assess call so they appear
    fully expanded in the assessment context.

    *assess_variant* overrides the settings-level ``assess_call_variant``
    when provided.
    """
    resolved_id = await db.resolve_page_id(page_id)
    if not resolved_id:
        log.warning("Could not resolve question page ID: %s", page_id)
        return
    page = await db.get_page(resolved_id)
    if not page:
        log.warning("Question page %s not found", page_id)
        return

    context_pages = await resolve_in_light_of(in_light_of, db)
    context_page_ids = [p.id for p in context_pages]

    assess_call = await db.create_call(
        CallType.ASSESS,
        scope_page_id=resolved_id,
        parent_call_id=call.id,
        context_page_ids=context_page_ids,
    )
    variant = assess_variant or get_settings().assess_call_variant
    cls = ASSESS_CALL_CLASSES[variant]
    assess = cls(resolved_id, assess_call, db)
    await assess.run()

    log.info(
        "Reassessed judgement for question %s (call %s)",
        resolved_id[:8],
        assess_call.id[:8],
    )

    await trace.record(
        ReassessTriggeredEvent(
            question_id=resolved_id,
            question_headline=page.headline,
            child_call_id=assess_call.id,
        )
    )


class ReassessedClaimItem(BaseModel):
    headline: str = Field(description=HEADLINE_DESCRIPTION)
    content: str = Field(description="Full standalone content of the replacement claim")
    credence: int = Field(
        description="Probability bucket 1-9 (1=very unlikely, 9=very likely)"
    )
    robustness: int = Field(
        description="Resilience of view 1-5 (1=fragile, 5=very robust)"
    )
    supersedes: list[str] = Field(
        default_factory=list,
        description=(
            "8-char short IDs of old claims this replaces. "
            "Consideration links are automatically copied from superseded claims."
        ),
    )


class LinkAddItem(BaseModel):
    claim_index: int = Field(
        description="Index into the claims list (0-based) identifying which new claim to link"
    )
    question_id: str = Field(description="8-char short ID of the question page")
    strength: float = Field(
        description="0-5: how strongly this claim bears on the question"
    )
    reasoning: str = Field(
        default="", description="Why this claim bears on the question"
    )


class LinkRemovalItem(BaseModel):
    link_id: str = Field(description="8-char short ID of the link to remove")
    reasoning: str = Field(default="", description="Why this link should be removed")


class ReassessedClaimsResult(BaseModel):
    claims: list[ReassessedClaimItem] = Field(
        description="Replacement claims — each optionally supersedes one or more old claims"
    )
    link_adds: list[LinkAddItem] = Field(
        default_factory=list,
        description="New consideration links to create between new claims and questions",
    )
    link_removals: list[LinkRemovalItem] = Field(
        default_factory=list,
        description="Existing links to delete",
    )


async def resolve_in_light_of(
    page_ids: Sequence[str], db: DB
) -> list[Page]:
    """Resolve page IDs for the in_light_of parameter.

    For each page_id: if it points to a question with an active judgement,
    return the latest judgement instead. Otherwise return the page itself.
    """
    resolved: list[Page] = []
    for pid in page_ids:
        full_id = await db.resolve_page_id(pid)
        if not full_id:
            log.warning("in_light_of: could not resolve page ID %s", pid)
            continue
        page = await db.get_page(full_id)
        if not page or not page.is_active():
            log.warning("in_light_of: page %s not found or inactive", pid)
            continue
        if page.page_type == PageType.QUESTION:
            judgements = await db.get_judgements_for_question(full_id)
            if judgements:
                latest = max(judgements, key=lambda j: j.created_at)
                resolved.append(latest)
                continue
        resolved.append(page)
    return resolved


async def reassess_claims(
    page_ids: Sequence[str],
    in_light_of: Sequence[str],
    guidance: str,
    call: Call,
    db: DB,
    trace: CallTrace,
) -> None:
    """Reassess N claims together, producing M replacement claims + link ops."""
    claim_pages: list[Page] = []
    for pid in page_ids:
        resolved_id = await db.resolve_page_id(pid)
        if not resolved_id:
            log.warning("reassess_claims: could not resolve %s", pid)
            continue
        page = await db.get_page(resolved_id)
        if page and page.is_active():
            claim_pages.append(page)
        else:
            log.warning("reassess_claims: page %s not found or inactive", pid)

    if not claim_pages:
        log.warning("reassess_claims: no valid claim pages found")
        return

    context_pages = await resolve_in_light_of(in_light_of, db)

    system_prompt = (
        (_PROMPTS_DIR / "preamble.md").read_text()
        + "\n\n"
        + (_PROMPTS_DIR / "reassess-claims.md").read_text()
        + "\n\n"
        + (_PROMPTS_DIR / "citations.md").read_text()
    )

    claims_text = "\n\n---\n\n".join(
        f"### `{p.id[:8]}` — {p.headline}\n\n"
        f"Credence: {p.credence}/9 | Robustness: {p.robustness}/5\n\n"
        f"{p.content}"
        for p in claim_pages
    )

    context_text_parts: list[str] = []
    for cp in context_pages:
        context_text_parts.append(
            f"### `{cp.id[:8]}` — {cp.headline} ({cp.page_type.value})\n\n"
            f"{cp.content}"
        )
    context_text = (
        "\n\n---\n\n".join(context_text_parts) if context_text_parts else ""
    )

    first_claim = claim_pages[0]
    ctx_result = await build_embedding_based_context(
        first_claim.headline, db
    )

    user_parts: list[str] = [
        f"## Workspace context\n\n{ctx_result.context_text}",
        f"## Claims to reassess\n\n{claims_text}",
    ]
    if context_text:
        user_parts.append(
            "## In light of (context pages)\n\n"
            "The following pages provide important context for this "
            "reassessment — e.g. new judgements on subquestions, or "
            "evidence that bears on the claims above.\n\n"
            f"{context_text}"
        )
    if guidance:
        user_parts.append(f"## Guidance\n\n{guidance}")

    user_message = "\n\n".join(user_parts)

    meta = LLMExchangeMetadata(
        call_id=call.id,
        phase="reassess_claims",
    )
    result = await structured_call(
        system_prompt,
        user_message=user_message,
        response_model=ReassessedClaimsResult,
        metadata=meta,
        db=db,
    )

    if result.parsed is None:
        log.warning("reassess_claims returned no data")
        return

    parsed = result.parsed

    new_pages: list[Page] = []
    superseded_by: dict[str, list[str]] = {}
    for item in parsed.claims:
        new_page = Page(
            page_type=PageType.CLAIM,
            layer=PageLayer.SQUIDGY,
            workspace=first_claim.workspace,
            content=item.content,
            headline=item.headline,
            credence=item.credence,
            robustness=item.robustness,
            provenance_model="claude-opus-4-6",
            provenance_call_type=call.call_type.value,
            provenance_call_id=call.id,
            project_id=first_claim.project_id,
        )
        await db.save_page(new_page)
        write_page_file(new_page)
        await extract_and_link_citations(new_page.id, new_page.content, db)

        old_ids: list[str] = []
        for superseded_short_id in item.supersedes:
            old_id = await db.resolve_page_id(superseded_short_id)
            if old_id:
                await db.supersede_page(old_id, new_page.id)
                await _copy_consideration_links(old_id, new_page.id, db)
                old_ids.append(old_id)
                log.info(
                    "reassess_claims: %s superseded by %s",
                    old_id[:8],
                    new_page.id[:8],
                )

        superseded_by[new_page.id] = old_ids
        new_pages.append(new_page)

    for link_add in parsed.link_adds:
        if link_add.claim_index < 0 or link_add.claim_index >= len(new_pages):
            log.warning(
                "reassess_claims: invalid claim_index %d", link_add.claim_index
            )
            continue
        claim_page = new_pages[link_add.claim_index]
        payload = LinkConsiderationPayload(
            claim_id=claim_page.id,
            question_id=link_add.question_id,
            strength=link_add.strength,
            reasoning=link_add.reasoning,
            role=LinkRole.DIRECT,
        )
        await execute_link_consideration(payload, call, db)

    for link_rm in parsed.link_removals:
        resolved_link_id = await db.resolve_link_id(link_rm.link_id)
        if not resolved_link_id:
            log.warning(
                "reassess_claims: could not resolve link ID %s", link_rm.link_id
            )
            continue
        payload = RemoveLinkPayload(
            link_id=resolved_link_id,
            reasoning=link_rm.reasoning,
        )
        await execute_remove_link(payload, call, db)

    log.info(
        "reassess_claims complete: %d input claims -> %d new claims, "
        "%d link adds, %d link removals",
        len(claim_pages),
        len(new_pages),
        len(parsed.link_adds),
        len(parsed.link_removals),
    )

    for new_page in new_pages:
        old_ids = superseded_by.get(new_page.id, [])
        for old_id in old_ids:
            await trace.record(
                ClaimReassessedEvent(
                    old_page_id=old_id,
                    new_page_id=new_page.id,
                    headline=new_page.headline,
                )
            )
        if not old_ids:
            await trace.record(
                ClaimReassessedEvent(
                    old_page_id="",
                    new_page_id=new_page.id,
                    headline=new_page.headline,
                )
            )


async def generate_abstracts(call: Call, db: DB) -> None:
    """Generate abstracts and embeddings for pages created in this call."""
    rows = (
        await db._execute(
            db.client.table("pages")
            .select("id, headline, content, page_type")
            .eq("provenance_call_id", call.id)
            .neq("page_type", "source")
        )
    )
    pages = [r for r in (rows.data or []) if r.get("id")]
    if not pages:
        log.info("No pages to abstract")
        return

    page_lines = "\n".join(
        f'- `{p["id"][:8]}`: "{p["headline"][:120]}"'
        for p in pages
    )
    user_message = (
        "Generate an abstract for each of the following pages.\n\n"
        f"{page_lines}\n\n"
        f"Abstract requirements: {ABSTRACT_INSTRUCTION}\n\n"
        "For each page, return its page_id and abstract."
    )

    page_contents = "\n\n---\n\n".join(
        f'Page `{p["id"][:8]}` — {p["headline"]}\n\n{p["content"]}'
        for p in pages
    )
    system_prompt = (
        "You are generating abstracts for workspace pages. "
        "You will be given page contents and must produce a self-contained "
        "abstract for each.\n\n"
        f"Page contents:\n\n{page_contents}"
    )

    meta = LLMExchangeMetadata(
        call_id=call.id,
        phase="abstract_generation",
    )
    result = await structured_call(
        system_prompt,
        user_message=user_message,
        response_model=_PageAbstractList,
        metadata=meta,
        db=db,
    )
    if result.parsed:
        await save_page_abstracts(result.parsed.summaries, db)
