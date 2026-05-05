"""Pairwise-preference judging task for versus.

Carved out of :mod:`rumil.versus_bridge`. The bridge module retains
:func:`judge_pair_orch` as a back-compat shim that delegates to
``run_versus(workflow=TwoPhaseWorkflow, task=JudgePairTask)``.

Public surface intentionally kept small:

- :class:`PairContext` — input shape for one pairwise judgment.
- :class:`JudgeArtifact` — structured output (verdict + label + reasoning).
- :class:`JudgePairTask` — the protocol implementation.

The five hash helpers (``compute_pair_surface_hash``,
``compute_tool_prompt_hash``, ``compute_closer_hash``) are module-level
because they hash code/template invariants that don't depend on inputs;
they're folded into :meth:`JudgePairTask.fingerprint`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, fields
from unittest.mock import MagicMock

from rumil.context import format_page, render_view
from rumil.database import DB
from rumil.models import (
    CallType,
    Page,
    PageDetail,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.versus_prompts import (
    build_system_prompt,
    compute_prompt_hash,
    extract_preference,
    label_to_verdict,
)
from rumil.workspace_exploration.explore import make_explore_subgraph_tool
from rumil.workspace_exploration.load_page import make_load_page_tool
from rumil.workspace_exploration.search import make_search_tool


@dataclass
class PairContext:
    """Inputs for one versus pairwise judgment.

    ``continuation_a_*`` / ``continuation_b_*`` are what the agent sees as
    "Continuation A" and "Continuation B" -- callers are responsible for
    putting them in display order (typically versus's deterministic
    ``order_pair``, so the same pair gets the same A/B assignment across
    all judges).

    ``source_a_id`` / ``source_b_id`` are the versus raw-pair source_ids
    in alphabetical order (versus's dedup-key convention). They are
    recorded as metadata on the Question page but NOT shown to the agent
    -- leaking the raw source_id (which can literally be ``"human"``)
    would break blind judging.
    """

    essay_id: str
    prefix_hash: str
    prefix_text: str
    continuation_a_id: str
    continuation_a_text: str
    continuation_b_id: str
    continuation_b_text: str
    source_a_id: str
    source_b_id: str
    task_name: str  # e.g. "general_quality", "grounding", "standalone_quality"


@dataclass
class JudgeArtifact:
    """Structured output of one pairwise judgment.

    ``verdict`` collapses the 7-point label into A/B/tie; ``preference_label``
    is the verbatim 7-point label the closer emitted; ``reasoning_text`` is
    the closer's full final-turn text, retained for audit / display.
    """

    verdict: str | None
    preference_label: str | None
    reasoning_text: str


def _build_headline(pair: PairContext) -> str:
    """Compose the Versus Question page headline.

    Intentionally source-free: ``pair.essay_id`` has the form
    ``<source>__<slug>``, so using it here would leak the source into
    headline embedding / search / tool output. ``prefix_hash[:8]``
    uniquely identifies the (essay, prefix_config) pair without
    disclosing where the essay came from.
    """
    return f"Versus judgment: {pair.task_name} [{pair.prefix_hash[:8]}]"


def _format_pair_content(pair: PairContext) -> str:
    # Intentionally do NOT disclose continuation source_ids -- they can
    # literally be "human" and would defeat blind judging. Source ids are
    # preserved only in the Question's extra metadata.
    return (
        "This question was created by the versus pairwise essay-judging harness. "
        "Two continuations of the same essay opening are compared on one dimension. "
        "Workspace material may be consulted if it bears on the essay's subject.\n\n"
        f"## Dimension\n\n{pair.task_name}\n\n"
        f"## Essay opening\n\n{pair.prefix_text}\n\n"
        f"## Continuation A\n\n{pair.continuation_a_text}\n\n"
        f"## Continuation B\n\n{pair.continuation_b_text}\n"
    )


def _build_abstract(pair: PairContext) -> str:
    """Compose the Versus Question's ``abstract``.

    Read by parent-context renderers (notably
    :func:`rumil.orchestrators.common.score_items_sequentially`) and
    by any rumil call type whose context builder renders the scope
    question at ``PageDetail.ABSTRACT`` (the current default for
    ``build_embedding_based_context``; see issue #448). Without the
    pair body here, view-creation and other research-phase calls
    that scope to a versus question see only the dimension framing
    and never the actual essay continuations they're meant to assess.

    Mirrors :func:`_format_pair_content` rather than carrying a
    scoring-specific instruction, so the same abstract reads
    sensibly across the call types that consume it. Inlining the
    full pair content (no truncation) avoids the silent mid-clause
    ``prefix_text[:N]`` concern that motivated the earlier
    framing-only version.
    """
    return (
        "Two continuations of the same essay opening are compared on one dimension. "
        "Workspace material may be consulted if it bears on the essay's subject.\n\n"
        f"## Dimension\n\n{pair.task_name}\n\n"
        f"## Essay opening\n\n{pair.prefix_text}\n\n"
        f"## Continuation A\n\n{pair.continuation_a_text}\n\n"
        f"## Continuation B\n\n{pair.continuation_b_text}\n"
    )


def _versus_extra(pair: PairContext) -> dict:
    # IMPORTANT: every key in page.extra is rendered verbatim by
    # rumil.context.format_page() (as "key: value" lines inline with
    # the page body). So anything disclosing source identity leaks
    # to the agent. Keep only neutral tags.
    #
    # `essay_id` is also excluded — its `<source>__<slug>` namespacing
    # bakes the source into what looks like a neutral id, and it's the
    # one field that can route a capable agent toward the essay's
    # origin via workspace material. Operator-side correlation goes
    # through `runs.config.essay_id` (non-agent-visible) and the
    # judgment row's `essay_id` keyed by `question_id`.
    return {
        "source": "versus",
        "prefix_hash": pair.prefix_hash,
        "task_name": pair.task_name,
    }


def _surface_hash_sentinel() -> dict[str, str]:
    """Build the sentinel dict from PairContext's actual fields, so
    adding/removing a field auto-updates the surface hash and hash
    coverage doesn't drift from the dataclass schema."""
    return {f.name: f"_SENTINEL_{f.name.upper()}_" for f in fields(PairContext)}


def compute_pair_surface_hash() -> str:
    """Short deterministic hash of the Versus Question page surface.

    Folded into the task fingerprint so structural edits to the
    agent-visible page surface auto-fork the dedup key without a
    manual version bump.

    Covers four surfaces together:

    - :func:`_build_headline` — the Question headline template.
    - :func:`_format_pair_content` — the Question body shape (section
      ordering, header text, etc.).
    - :func:`_build_abstract` — the Question abstract template
      (read by parent-scoring; surfaces in agent context).
    - :func:`_versus_extra` — the set of keys stored on ``page.extra``
      (values are pair-dependent and live in the content body instead;
      only the key schema is hashed).

    Scope: orch / tools paths only. The blind path (single-turn LLM
    call, no DB) doesn't read the Question page, so a page-surface
    edit there wouldn't affect blind judgments.
    """
    sentinel = PairContext(**_surface_hash_sentinel())
    blob = json.dumps(
        {
            "headline": _build_headline(sentinel),
            "content": _format_pair_content(sentinel),
            "abstract": _build_abstract(sentinel),
            "extra_keys": sorted(_versus_extra(sentinel).keys()),
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


_TOOL_SERVER_NAME = "versus-judge-tools"


def compute_tool_prompt_hash() -> str:
    """Short deterministic hash of the workspace-exploration tool prompts.

    Hashes the ``{tool_name: description_string}`` map for the three
    tools the rumil bridge exposes to the orch closer
    (``search_workspace``, ``load_page``, ``explore_subgraph``). Folded
    into the task fingerprint so edits to those tool docstrings fork
    the dedup key.

    Scope decision (documented so it doesn't drift): this covers only
    the workspace-exploration family — the tools directly passed to
    the SDK in the closer. The orchestrator's dispatched calls inside
    the workflow use a broader tool set (find_considerations, assess,
    scout-*, etc.) that isn't passed from here; those are covered by
    ``code_fingerprint`` over the orchestrators / calls / prompts
    directories.
    """
    db_stub = MagicMock()
    trace_stub = MagicMock()
    search_tool = make_search_tool(db_stub, trace_stub)
    load_page_tool = make_load_page_tool(db_stub, trace_stub)
    explore_tool = make_explore_subgraph_tool(db_stub, trace_stub, questions_only=False)
    descriptions = {
        search_tool.name: search_tool.description,
        load_page_tool.name: load_page_tool.description,
        explore_tool.name: explore_tool.description,
    }
    blob = json.dumps(descriptions, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


_CLOSER_USER_PROMPT_TEMPLATE = (
    "A research run has just finished investigating the pair comparison "
    "captured in the scope question. The rendered question (including "
    "the essay prefix, both continuations, the considerations and "
    "judgements the orchestrator produced, and the distilled view "
    "items) follows; your job is to read it, weigh what the research "
    "surfaced, and emit the 7-point preference label. You have the "
    "workspace tools if further material bears on the essay's subject, "
    "but keep usage light — this is the closing step, not a fresh "
    "investigation.\n\n"
    "{rendered}\n\n"
    "End your response with one of the 7-point preference labels on its "
    "own line."
)
_CLOSER_SDK_MAX_TURNS = 5
_CLOSER_DISALLOWED_TOOLS = ("Write", "Edit", "Glob")
_CLOSER_RENDER_DETAIL = "CONTENT"
_CLOSER_RENDER_LINKED_DETAIL = "CONTENT"
_CLOSER_RENDER_MIN_IMPORTANCE = 2


def compute_closer_hash() -> str:
    """Short deterministic hash of the closer's invariant config.

    Covers the parts of the closer agent that the prompt-hash and
    tool-prompt-hash don't: the inline user-prompt template, the
    SDK agent's max_turns budget, the disallowed-tools set, and the
    rendering knobs the closer reads (page detail level, linked
    detail, view min_importance). Folded into the task fingerprint
    so an edit here auto-forks the dedup key.

    orch-only — the blind path doesn't have a closer step.
    """
    blob = json.dumps(
        {
            "user_prompt_template": _CLOSER_USER_PROMPT_TEMPLATE,
            "sdk_agent_max_turns": _CLOSER_SDK_MAX_TURNS,
            "disallowed_tools": list(_CLOSER_DISALLOWED_TOOLS),
            "render_detail": _CLOSER_RENDER_DETAIL,
            "render_linked_detail": _CLOSER_RENDER_LINKED_DETAIL,
            "render_min_importance": _CLOSER_RENDER_MIN_IMPORTANCE,
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


class JudgePairTask:
    """Pairwise-preference judging task — produces a 7-point label."""

    name = "judge_pair"
    call_type: CallType = CallType.VERSUS_JUDGE
    # Defaults shared with the closer machinery in rumil.versus_closer.
    sdk_max_turns: int = _CLOSER_SDK_MAX_TURNS
    disallowed_tools: tuple[str, ...] = _CLOSER_DISALLOWED_TOOLS
    tool_server_name: str = _TOOL_SERVER_NAME

    def __init__(self, *, dimension: str, dimension_body: str):
        self.dimension = dimension
        self.dimension_body = dimension_body

    def fingerprint(self, inputs: PairContext) -> Mapping[str, str | int | bool | None]:
        return {
            "kind": self.name,
            "dimension": self.dimension,
            "prompt_hash": compute_prompt_hash(self.dimension_body, with_tools=True),
            "tool_prompt_hash": compute_tool_prompt_hash(),
            "pair_surface_hash": compute_pair_surface_hash(),
            "closer_hash": compute_closer_hash(),
        }

    async def create_question(self, db: DB, inputs: PairContext) -> str:
        """Create a fresh Question page for this pair. Returns the page id.

        No reuse: each judgment invocation gets its own question. Dedup
        happens one layer up (at the versus_judgments level), so in
        practice we only create a question when a judgment is actually
        pending. Tagged ``extra.source="versus"`` for filterability;
        raw source ids stay in ``extra`` only (and even those are
        excluded — see :func:`_versus_extra`).
        """
        page = Page(
            page_type=PageType.QUESTION,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content=_format_pair_content(inputs),
            headline=_build_headline(inputs),
            abstract=_build_abstract(inputs),
            project_id=db.project_id,
            provenance_model="versus-bridge",
            provenance_call_type=self.call_type.value,
            run_id=db.run_id,
            extra=_versus_extra(inputs),
        )
        await db.save_page(page)
        return page.id

    async def render_for_closer(self, db: DB, question_id: str) -> str:
        """Render a Question + the workflow's research into the closer prompt.

        ``format_page`` on a Question already surfaces considerations and
        judgements as linked items, but only at HEADLINE detail and without
        the View / view_items the orchestrator synthesizes. That made the
        closer effectively read preformed claim titles with no evidence and
        ignore the most distilled layer of research it had paid for.

        This helper extends the standard Question rendering along three
        axes: considerations and judgements render at CONTENT detail
        (claim body + link reasoning), and the active View page + all of
        its view_items are rendered below via :func:`render_view` at
        ``min_importance=2`` so every item the orch wrote is visible. Kept
        versus-specific to avoid changing how the non-versus
        orchestrator / call-site code paths render Question pages.
        """
        question = await db.get_page(question_id)
        if question is None:
            raise RuntimeError(f"question {question_id} missing after orch run")
        view = await db.get_view_for_question(question_id)
        # Exclude the view from format_page's linked rendering so it
        # isn't double-rendered: format_page surfaces the active view as
        # "Current take on this question" at CONTENT detail (~3 paragraphs
        # of view content), and we then render the view in full via
        # render_view below. The dedicated render is the canonical one
        # for the closer (carries the view items at min_importance=2).
        exclude = {view.id} if view is not None else None
        body = await format_page(
            question,
            PageDetail.CONTENT,
            linked_detail=PageDetail.CONTENT,
            db=db,
            exclude_page_ids=exclude,
        )
        if view is None:
            return body
        items = await db.get_view_items(view.id, min_importance=2)
        view_rendered = await render_view(view, items, min_importance=2)
        return f"{body}\n\n{view_rendered}"

    def closer_prompts(self, rendered: str, inputs: PairContext) -> tuple[str, str]:
        system = build_system_prompt(self.dimension_body, with_tools=True)
        user = _CLOSER_USER_PROMPT_TEMPLATE.format(rendered=rendered)
        return system, user

    def extract_artifact(self, closer_text: str) -> JudgeArtifact:
        label = extract_preference(closer_text)
        return JudgeArtifact(
            verdict=label_to_verdict(label),
            preference_label=label,
            reasoning_text=closer_text,
        )
