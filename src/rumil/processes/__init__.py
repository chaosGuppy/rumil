"""Process abstraction: typed, uniform interface over research workflows.

A ``Process`` takes a ``Scope`` and a ``BudgetEnvelope`` and produces a
``Result[Delta]`` — a typed graph-delta plus follow-up signals plus
resource usage and a completion status. Individual process types
(Investigator, Robustifier, Surveyor, ...) specialise the delta shape.

This package currently ships three concrete processes, all v1:

- ``Investigator`` — wraps ``TwoPhaseOrchestrator``; produces a
  ``ViewDelta``.
- ``Robustifier`` — wraps ``RobustifyOrchestrator``; produces a
  ``VariantSetDelta``.
- ``Surveyor`` — new minimal implementation (not a wrap); produces a
  ``MapDelta`` alongside rich ``FollowUp`` signals.

See ``scope.py``, ``budget.py``, ``delta.py``, ``signals.py``,
``result.py`` for the shared data types.
"""

from rumil.processes.budget import BudgetEnvelope, ResourceUsage
from rumil.processes.delta import (
    Delta,
    LinkRef,
    MapDelta,
    PageRef,
    SupersedeRef,
    VariantSetDelta,
    ViewDelta,
)
from rumil.processes.investigator import Investigator
from rumil.processes.result import (
    Continuation,
    InvestigatorResult,
    Result,
    RobustifierResult,
    Status,
    SurveyorResult,
)
from rumil.processes.robustifier import Robustifier
from rumil.processes.scope import (
    ClaimScope,
    ProjectScope,
    QuestionScope,
    Scope,
    SubgraphScope,
)
from rumil.processes.signals import (
    ConsolidateRequest,
    ElicitInput,
    FocusRequest,
    FollowUp,
    PropagateFromChange,
    ReassessRequest,
    RobustifyRequest,
)
from rumil.processes.surveyor import Surveyor

__all__ = [
    "BudgetEnvelope",
    "ClaimScope",
    "ConsolidateRequest",
    "Continuation",
    "Delta",
    "ElicitInput",
    "FocusRequest",
    "FollowUp",
    "Investigator",
    "InvestigatorResult",
    "LinkRef",
    "MapDelta",
    "PageRef",
    "ProjectScope",
    "PropagateFromChange",
    "QuestionScope",
    "ReassessRequest",
    "ResourceUsage",
    "Result",
    "Robustifier",
    "RobustifierResult",
    "RobustifyRequest",
    "Scope",
    "Status",
    "SubgraphScope",
    "SupersedeRef",
    "Surveyor",
    "SurveyorResult",
    "VariantSetDelta",
    "ViewDelta",
]
