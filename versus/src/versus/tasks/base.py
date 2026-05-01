"""``VersusTask`` protocol: the per-task half of the universal versus runner.

A ``VersusTask`` describes one shape of work versus can dedup, fingerprint,
and drive end-to-end through ``run_versus``. The :class:`Workflow` half
(see :mod:`rumil.versus_workflow`) covers *how* the run executes
(orchestrator dispatch, budget); ``VersusTask`` covers *what* the run
is about (creating the scope question, rendering it for the closer,
extracting the artifact afterwards).

Today's only concrete implementation is :class:`JudgePairTask` (pairwise
preference judging). #426 will add ``CompleteEssayTask`` for the
completion path.

Invariants worth preserving across implementations:

- ``fingerprint(inputs)`` is the dedup primitive. It must be a stable
  function of (task code, inputs) — folded into ``make_versus_config``'s
  ``task`` subdict and from there into the row-level ``config_hash``.
  Adding a new task knob means adding it to ``fingerprint``.
- ``create_question`` writes one Question page (the run's scope) and
  returns its id. The shape of that page (headline, body, extra) is
  task-specific; the runner doesn't care.
- ``render_for_closer`` runs after the workflow, AFTER any subgraph
  the workflow built has been persisted. It returns the prompt the
  closer agent will read; the runner threads it into ``closer_prompts``.
- ``closer_prompts(rendered, inputs)`` returns ``(system, user)``. The
  system prompt is what the SDK agent sees; the user prompt is the
  initial message. Tasks own this — the runner is task-agnostic.
- ``extract_artifact(text)`` parses the closer agent's final text into
  whatever structured artifact this task produces (a verdict, a
  completion, etc.). Pure function; no DB.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, TypeVar, runtime_checkable

from rumil.database import DB

TInputs = TypeVar("TInputs")
TArtifact = TypeVar("TArtifact")


@runtime_checkable
class VersusTask(Protocol[TInputs, TArtifact]):
    """One pluggable versus task — together with a Workflow, fully drives a run."""

    name: str

    def fingerprint(self, inputs: TInputs) -> Mapping[str, str | int | bool | None]: ...

    async def create_question(self, db: DB, inputs: TInputs) -> str: ...

    async def render_for_closer(self, db: DB, question_id: str) -> str: ...

    def closer_prompts(self, rendered: str, inputs: TInputs) -> tuple[str, str]: ...

    def extract_artifact(self, closer_text: str) -> TArtifact: ...
