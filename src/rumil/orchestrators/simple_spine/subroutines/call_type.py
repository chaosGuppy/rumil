"""CallTypeSubroutine — wrap an existing CallRunner inside a staged sub-DB.

The wrapped CallType (find_considerations, scout_*, web_research, etc.)
runs in a per-spawn :class:`DB` with a fresh ``run_id`` and ``staged=True``.
Pages it creates are tagged with that run_id and ``staged=true``, so
they remain inspectable in the trace UI but never leak into the
baseline workspace.

The mainline agent receives a text summary of pages created (headline
+ short content) — not the pages themselves. If the wrapped CallType
mutates pre-existing baseline pages (rare for read-mostly scouts),
those mutations land as mutation_events tagged with the sub-run and
visible only to the staged sub-DB.

**Caveats** — not every CallType is staged-safe today:

- Calls that read the workspace's prio-budget pool may misbehave with
  ``init_budget`` set in isolation.
- Calls that emit cross-run notifications (broadcaster, view refresh)
  will fire from the sub-run's identity; the parent's frontend trace
  UI will surface them under the sub-run's call tree.

Start with ``find_considerations`` and the scout family — they are
purely additive and well-behaved in staged mode.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from rumil.calls.stages import CallRunner
from rumil.database import DB
from rumil.models import CallType, Page
from rumil.orchestrators.simple_spine.subroutines.base import (
    SpawnCtx,
    SubroutineBase,
    SubroutineResult,
)


@dataclass(frozen=True, kw_only=True)
class CallTypeSubroutine(SubroutineBase):
    """Wrap an existing rumil CallRunner inside a staged sub-DB.

    Inherits cross-cutting fields from :class:`SubroutineBase`. The
    wrapped CallRunner fires its own LLM calls with its own
    prompts/models — this SubroutineDef just specifies which rumil
    CallType to wrap.
    """

    call_type: CallType
    runner_cls: type[CallRunner]
    base_max_rounds: int = 5
    base_budget: int = 1
    overridable: frozenset[str] = field(default_factory=lambda: frozenset({"intent", "max_rounds"}))

    def __post_init__(self) -> None:
        if self.base_token_cap is not None:
            raise ValueError(
                f"CallTypeSubroutine {self.name!r}: base_token_cap is "
                "inert on call_type kinds — the wrapped CallRunner makes "
                "LLM calls through a path that doesn't tap the SimpleSpine "
                "BudgetClock; budgeting is via init_budget on the staged "
                "sub-DB. Drop base_token_cap from this entry."
            )
        if "token_cap" in self.overridable:
            raise ValueError(
                f"CallTypeSubroutine {self.name!r}: 'token_cap' in "
                "overridable is inert (see base_token_cap). Drop it."
            )
        if self.consumes:
            raise ValueError(
                f"CallTypeSubroutine {self.name!r}: consumes is not yet "
                "supported on call_type kinds — the wrapped CallRunner "
                "uses its own context-builder, not the spine's spawn "
                "user-prompt rendering. Artifact integration here needs "
                "a separate design and is out of MVP scope."
            )

    def _supports_include_artifacts(self) -> bool:
        return False

    def fingerprint(self) -> Mapping[str, Any]:
        out = dict(super().fingerprint())
        out["kind"] = "call_type"
        out["call_type"] = self.call_type.value
        out["runner_cls"] = self.runner_cls.__name__
        out["base_max_rounds"] = self.base_max_rounds
        out["base_budget"] = self.base_budget
        return out

    def _default_intent_description(self) -> str:
        return (
            "Brief statement of what you want this call to investigate. "
            "Recorded on the trace; the underlying CallType uses its "
            "own context-builder to drive the work."
        )

    def _extra_schema_properties(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "max_rounds" in self.overridable:
            out["max_rounds"] = {
                "type": "integer",
                "minimum": 1,
                "maximum": self.base_max_rounds,
                "description": (
                    f"Cap rounds (default {self.base_max_rounds}). Each round "
                    "consumes one budget unit in the spawned sub-DB."
                ),
            }
        return out

    async def run(self, ctx: SpawnCtx, overrides: Mapping[str, Any]) -> SubroutineResult:
        max_rounds_override = overrides.get("max_rounds")
        max_rounds = (
            int(max_rounds_override)
            if max_rounds_override is not None and "max_rounds" in self.overridable
            else self.base_max_rounds
        )

        sub_run_id = str(uuid.uuid4())
        sub_db = await DB.create(
            run_id=sub_run_id,
            prod=ctx.db._prod,
            project_id=ctx.db.project_id,
            staged=True,
        )
        try:
            await sub_db.create_run(
                name=f"simple_spine_spawn:{self.name}",
                question_id=ctx.question_id,
                config={
                    "parent_call_id": ctx.parent_call_id,
                    "parent_run_id": ctx.db.run_id,
                    "subroutine_name": self.name,
                },
            )
            await sub_db.init_budget(self.base_budget)
            # Honor inherit_assumptions by appending the operating
            # assumptions to the question's content in the staged sub-DB.
            # The wrapped CallRunner reads the question via its
            # context-builder, so the assumptions reach the LLM through
            # the natural context-rendering path. The mutation is
            # recorded as a staged-only event so the baseline question
            # is untouched. Pre-gate avoids a no-op DB read+write when
            # there's nothing to splice.
            if self.inherit_assumptions and ctx.operating_assumptions.strip():
                question = await sub_db.get_page(ctx.question_id)
                if question is not None:
                    augmented = self.apply_assumptions(question.content, ctx)
                    await sub_db.update_page_content(ctx.question_id, augmented)
            call = await sub_db.create_call(
                self.call_type,
                scope_page_id=ctx.question_id,
            )
            runner = self.runner_cls(
                ctx.question_id,
                call,
                sub_db,
                broadcaster=ctx.broadcaster,
                max_rounds=max_rounds,
            )
            await runner.run()
            new_pages = await _query_pages_for_call(sub_db, call.id)
            summary = _format_pages_summary(self.name, new_pages)
            return SubroutineResult(
                text_summary=summary,
                extra={
                    "sub_run_id": sub_run_id,
                    "sub_call_id": call.id,
                    "pages_created": len(new_pages),
                },
            )
        finally:
            await sub_db.close()


async def _query_pages_for_call(db: DB, call_id: str) -> Sequence[Page]:
    """Pages created by ``call_id`` in the staged sub-DB.

    We deliberately don't fold mutation events here — the sub-run's
    fresh identity means there are no relevant ones, and the staged
    filter on the page query is sufficient to scope to this fork.
    """
    from rumil.database import _SLIM_PAGE_COLUMNS, _row_to_page, _rows

    query = (
        db.client.table("pages")
        .select(_SLIM_PAGE_COLUMNS)
        .eq("provenance_call_id", call_id)
        .order("created_at")
    )
    if db.project_id:
        query = query.eq("project_id", db.project_id)
    result = await db._execute(query)
    return [_row_to_page(r) for r in _rows(result)]


def _format_pages_summary(name: str, pages: Sequence[Page]) -> str:
    if not pages:
        return f"# {name}\n_(no pages created)_"
    lines: list[str] = [f"# {name} — {len(pages)} pages created", ""]
    for p in pages:
        lines.append(f"## {p.page_type.value}: {p.headline}")
        if p.content:
            content = p.content.strip()
            if len(content) > 1500:
                content = content[:1500] + "\n…[truncated]"
            lines.append(content)
        lines.append("")
    return "\n".join(lines)
