"""Result: the uniform return shape for every process run.

A Process's ``run()`` returns a ``Result[TDelta]`` carrying:

- ``delta``: the typed mutation the process committed (``ViewDelta`` /
  ``VariantSetDelta`` / ``MapDelta``). Always valid for its shape even
  when the process didn't finish — ``status`` carries the completion
  story.
- ``signals``: typed follow-up recommendations for a scheduler or
  human. No consumer yet; emitted for observability and to exercise the
  output surface the Surveyor process needs.
- ``usage``: actual resource consumption across the budget dimensions.
- ``status``: complete / incomplete / failed. ``incomplete`` means the
  process ran, spent what it was given, and stopped with a partial
  delta; the delta is still valid.
- ``continuation``: opaque resumption state. Populated only when
  ``status == "incomplete"`` and the process supports resume. v1 never
  populates this; the field is reserved for future resumption support.
- ``self_report``: short free-text summary of what the process thinks
  it did, for display and debugging.
"""

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel

from rumil.processes.budget import ResourceUsage
from rumil.processes.delta import Delta, MapDelta, VariantSetDelta, ViewDelta
from rumil.processes.signals import FollowUp

Status = Literal["complete", "incomplete", "failed"]


class Continuation(BaseModel):
    """Opaque process-specific resumption state. Unused in v1."""

    process_type: str
    data: dict = {}


TDelta = TypeVar("TDelta", bound=Delta)


class Result(BaseModel, Generic[TDelta]):
    process_type: str
    run_id: str
    delta: TDelta
    signals: list[FollowUp] = []
    usage: ResourceUsage
    status: Status
    continuation: Continuation | None = None
    self_report: str = ""


InvestigatorResult = Result[ViewDelta]
RobustifierResult = Result[VariantSetDelta]
SurveyorResult = Result[MapDelta]
