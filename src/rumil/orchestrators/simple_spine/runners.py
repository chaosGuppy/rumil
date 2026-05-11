"""Registry mapping YAML ``call_type_key`` strings to runtime objects.

YAML configs reference rumil ``CallRunner`` subclasses by name (since
class objects don't serialize). The registry resolves a string key like
``"find_considerations"`` to ``(CallType.FIND_CONSIDERATIONS,
FindConsiderationsCall)`` for use by :class:`CallTypeSubroutine`.

Built-in keys (registered lazily on first access so heavy imports don't
land at module load time): ``find_considerations``,
``scout_subquestions``, ``scout_estimates``, ``scout_hypotheses``,
``scout_analogies``, ``scout_paradigm_cases``, ``scout_factchecks``,
``scout_web_questions``, ``web_research``. Add more via
:func:`register_call_type` or extend :func:`_register_builtins`.
"""

from __future__ import annotations

from rumil.calls.stages import CallRunner
from rumil.models import CallType

_REGISTRY: dict[str, tuple[CallType, type[CallRunner]]] = {}
_BUILTINS_LOADED = False


def register_call_type(key: str, call_type: CallType, runner_cls: type[CallRunner]) -> None:
    """Register ``(CallType, runner_cls)`` under ``key`` for YAML lookup.

    Idempotent: re-registering silently overwrites.
    """
    _REGISTRY[key] = (call_type, runner_cls)


def get_call_type(key: str) -> tuple[CallType, type[CallRunner]]:
    """Resolve a YAML ``call_type_key`` to its CallType + runner class."""
    if not _BUILTINS_LOADED:
        _register_builtins()
    if key not in _REGISTRY:
        known = sorted(_REGISTRY)
        raise KeyError(f"unknown call_type_key {key!r}; registered: {known}")
    return _REGISTRY[key]


def list_call_types() -> list[str]:
    if not _BUILTINS_LOADED:
        _register_builtins()
    return sorted(_REGISTRY)


def _register_builtins() -> None:
    """Lazy import + register a curated set of staged-safe call types.

    "Staged-safe" means the call is purely additive (creates pages,
    doesn't mutate global state, doesn't depend on the workspace prio
    pool) — see ``CallTypeSubroutine`` docstring for caveats.
    """
    global _BUILTINS_LOADED
    from rumil.calls.find_considerations import FindConsiderationsCall
    from rumil.calls.scout_analogies import ScoutAnalogiesCall
    from rumil.calls.scout_estimates import ScoutEstimatesCall
    from rumil.calls.scout_factchecks import ScoutFactchecksCall
    from rumil.calls.scout_hypotheses import ScoutHypothesesCall
    from rumil.calls.scout_paradigm_cases import ScoutParadigmCasesCall
    from rumil.calls.scout_subquestions import ScoutSubquestionsCall
    from rumil.calls.scout_web_questions import ScoutWebQuestionsCall
    from rumil.calls.web_research import WebResearchCall

    register_call_type("find_considerations", CallType.FIND_CONSIDERATIONS, FindConsiderationsCall)
    register_call_type("scout_subquestions", CallType.SCOUT_SUBQUESTIONS, ScoutSubquestionsCall)
    register_call_type("scout_estimates", CallType.SCOUT_ESTIMATES, ScoutEstimatesCall)
    register_call_type("scout_hypotheses", CallType.SCOUT_HYPOTHESES, ScoutHypothesesCall)
    register_call_type("scout_analogies", CallType.SCOUT_ANALOGIES, ScoutAnalogiesCall)
    register_call_type(
        "scout_paradigm_cases", CallType.SCOUT_PARADIGM_CASES, ScoutParadigmCasesCall
    )
    register_call_type("scout_factchecks", CallType.SCOUT_FACTCHECKS, ScoutFactchecksCall)
    register_call_type("scout_web_questions", CallType.SCOUT_WEB_QUESTIONS, ScoutWebQuestionsCall)
    register_call_type("web_research", CallType.WEB_RESEARCH, WebResearchCall)
    _BUILTINS_LOADED = True
