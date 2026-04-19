"""Build Model call: synthesize a structured theoretical model for a question.

# Design

The call produces a single MODEL page whose body is structured Markdown
covering variables, relations, parameters, predictions, assumptions, and
sensitivities. Predictions are also emitted as *separate* pages —
either CLAIM pages linked to the target question via LINK_CONSIDERATION,
or VIEW_ITEM proposals if the target question already has a View. This
design choice matters: the MODEL page is a single unit that can be
superseded wholesale on subsequent `build_model` calls, which matches how
a model is typically revised (all variables and relations get updated
together). Predictions, by contrast, need to be attackable one-at-a-time
by downstream scouts (scout_c_how_false, scout_c_stress_test_cases), so
they live as their own pages with their own epistemic scores.

Flavors
-------

Only the `theoretical` flavor is implemented today — no code execution,
no sandbox, no subprocesses. The `flavor` parameter is plumbed through
as an extension point. TODO(model-building/executable): the executable
flavor (numpy/scipy in a sandbox, plot artifacts) depends on the
sandboxing design doc which is being drafted separately. When that lands,
add an `EXECUTABLE` value to `ModelFlavor` and gate the sandboxed code
path on `self._flavor`.

Shape of the call
-----------------

1. `build_context` — standard embedding-based context on the scope
   question. Knows whether the question already has a View, so the prompt
   can steer predictions into `propose_view_item` vs `create_claim`.
2. `update_workspace` — pre-creates a MODEL page with a placeholder body
   and a headline derived from the question, writes a MODEL_OF link back
   to the question, then runs a single-pass agent loop. The agent is
   expected to call `write_model_body` exactly once, followed by one
   `create_claim` (or `propose_view_item`) per prediction and appropriate
   `link_consideration` moves.
3. `closing_review` — standard review + self-assessment.

Staged-runs pattern
-------------------

All mutations go through the existing DB helpers:
- `save_page` / `save_link` for the MODEL page and MODEL_OF link (new
  rows, tagged staged/run_id as usual).
- `supersede_page` for replacing a prior model on the same question
  (records a `supersede_page` mutation event; dual-writes when not
  staged).
- `update_page_content` + `update_epistemic_score` inside the
  `write_model_body` move (both record mutation events).

Reading the MODEL page from a staged run therefore sees the run's own
version via the standard `_staged_filter` + `_apply_page_events` path.
"""

from __future__ import annotations

import logging

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import EmbeddingContext
from rumil.calls.page_creators import SimpleAgentLoop
from rumil.calls.stages import (
    CallRunner,
    ClosingReviewer,
    ContextBuilder,
    WorkspaceUpdater,
)
from rumil.database import DB
from rumil.models import (
    Call,
    CallStage,
    CallType,
    LinkType,
    ModelFlavor,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.settings import get_settings

log = logging.getLogger(__name__)


_MODEL_BODY_PLACEHOLDER = (
    "_This MODEL page is a stub. The build_model agent will fill it in "
    "during this call via the `write_model_body` tool._"
)


class BuildModelCall(CallRunner):
    """Produce a structured theoretical model for a question."""

    context_builder_cls = EmbeddingContext
    workspace_updater_cls = SimpleAgentLoop
    closing_reviewer_cls = StandardClosingReview  # type: ignore[assignment]
    call_type = CallType.BUILD_MODEL

    def __init__(
        self,
        question_id: str,
        call: Call,
        db: DB,
        *,
        flavor: ModelFlavor = ModelFlavor.THEORETICAL,
        broadcaster=None,
        up_to_stage: CallStage | None = None,
    ) -> None:
        self._flavor = flavor
        self._model_id: str = ""
        self._has_view: bool = False
        call.call_params = {
            **(call.call_params or {}),
            "flavor": flavor.value,
        }
        super().__init__(question_id, call, db, broadcaster=broadcaster, up_to_stage=up_to_stage)

    async def _run_stages(self) -> None:
        if self._flavor != ModelFlavor.THEORETICAL:
            raise NotImplementedError(
                f"BuildModelCall flavor {self._flavor!r} is not implemented. "
                "Only 'theoretical' is supported; the executable flavor is "
                "deferred pending a separate sandboxing design doc."
            )
        self._model_id = await self._create_model_page()
        existing_view = await self.infra.db.get_view_for_question(self.infra.question_id)
        self._has_view = existing_view is not None
        self.workspace_updater = self._make_workspace_updater()
        self.closing_reviewer = self._make_closing_reviewer()
        await super()._run_stages()

    async def _create_model_page(self) -> str:
        """Create the MODEL page and MODEL_OF link before the agent loop runs.

        If the question already has an active MODEL page, supersede it. This
        mirrors CreateViewCall's supersession handling so that running
        build_model twice on the same question cleanly replaces the prior
        model as a single unit.
        """
        existing_model = await self._get_active_model_for_question(self.infra.question_id)

        question = await self.infra.db.get_page(self.infra.question_id)
        q_headline = question.headline if question else self.infra.question_id[:8]

        model = Page(
            page_type=PageType.MODEL,
            layer=PageLayer.WIKI,
            workspace=Workspace.RESEARCH,
            content=_MODEL_BODY_PLACEHOLDER,
            headline=f"Model: {q_headline}",
            provenance_call_type=self.call_type.value,
            provenance_call_id=self.infra.call.id,
            provenance_model=get_settings().model,
            extra={"flavor": self._flavor.value},
        )
        await self.infra.db.save_page(model)
        await self.infra.db.save_link(
            PageLink(
                from_page_id=model.id,
                to_page_id=self.infra.question_id,
                link_type=LinkType.MODEL_OF,
            )
        )

        if existing_model is not None:
            await self.infra.db.supersede_page(existing_model.id, model.id)
            log.info(
                "Superseded old model %s with new model %s",
                existing_model.id[:8],
                model.id[:8],
            )

        log.info(
            "Created model page %s for question %s (flavor=%s)",
            model.id[:8],
            self.infra.question_id[:8],
            self._flavor.value,
        )
        return model.id

    async def _get_active_model_for_question(self, question_id: str) -> Page | None:
        """Return the active (non-superseded) MODEL page for a question, or None.

        No RPC yet — this is a single batched query (MODEL_OF links to the
        question, then one page fetch), so it's O(1) round trips regardless
        of how many historical models exist for the question.
        """
        links = await self.infra.db.get_links_to(question_id)
        model_link_sources = [
            link.from_page_id for link in links if link.link_type == LinkType.MODEL_OF
        ]
        if not model_link_sources:
            return None
        pages = await self.infra.db.get_pages_by_ids(model_link_sources)
        for pid in model_link_sources:
            page = pages.get(pid)
            if page and not page.is_superseded and page.page_type == PageType.MODEL:
                return page
        return None

    def _make_context_builder(self) -> ContextBuilder:
        return EmbeddingContext(self.call_type)

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return SimpleAgentLoop(
            self.call_type,
            self.task_description(),
            available_moves=self._resolve_available_moves(),
        )

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return StandardClosingReview(self.call_type)

    def task_description(self) -> str:
        view_note = (
            "\n\nThe scope question already has a View. Emit predictions via "
            "`propose_view_item` so they are triaged into the View by the "
            "next assess/update_view call."
            if self._has_view
            else "\n\nThe scope question does not have a View. Emit predictions "
            "as CLAIM pages and link each one to the scope question with "
            "`link_consideration` (or use the inline `links` field on "
            "`create_claim`)."
        )
        return (
            "Build a structured theoretical model of the phenomenon this "
            "question is about.\n\n"
            f"Question ID: `{self.infra.question_id}`\n"
            f"Model page ID: `{self._model_id}`\n"
            f"Flavor: {self._flavor.value}\n\n"
            "Steps:\n"
            "1. Call `write_model_body` exactly once with the full model "
            "body and a robustness score for the model as a whole.\n"
            "2. Emit each quantitative prediction as a separate page so that "
            "downstream scouts (scout_c_how_false, scout_c_stress_test_cases) "
            "can attack it."
            f"{view_note}"
        )
