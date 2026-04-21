"""Per-node prioritiser substrate.

V1 scope: provide a run-scoped registry that dedups prioritisation work
across orchestrator instances on the same DB. The registry is shared
across all forks of a root DB so that two orchestrators operating
against the same ``tmp_db`` (or the same production ``DB`` root) see
each other's work.

V1 guarantees two concrete invariants via the registry:

* **Top-level dedup.** At most one orchestrator runs its body on a
  given question per registry. Parallel / sequential calls to
  ``TwoPhaseOrchestrator.run(Q)`` on the same root DB share a single
  completion future.
* **Non-scope dispatch dedup.** When an orchestrator with scope ``S``
  dispatches a call on a different question ``T``, the registry records
  ``(T, call_type)``. Any subsequent non-scope dispatch on the same
  target/type from any orchestrator is skipped.

The actor substrate (``Prioritiser`` round loop, ``PrioritiserRegistry.recurse``,
subclass ``_fire_subscription`` deliverables) is in place as the V2
landing surface. The V1 facades still drive their own round loops; V2
will cut them over to the actor model.
"""

from rumil.prioritisers.prioritiser import Prioritiser
from rumil.prioritisers.registry import PrioritiserRegistry
from rumil.prioritisers.subscription import Subscription

__all__ = [
    "Prioritiser",
    "PrioritiserRegistry",
    "Subscription",
]
