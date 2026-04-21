"""ExperimentalQuestionPrioritiser: V2 landing spot for ExperimentalOrchestrator.

V1 scaffolding. Matches ``QuestionPrioritiser`` but with
``summarise_before_assess = False`` as a class attribute so
``GlobalPrioOrchestrator``'s class-attribute lookup resolves correctly
(mirrors ``ExperimentalOrchestrator.summarise_before_assess``).

Future V2 scope:

* Linker subsystem (``_run_subquestion_linker`` + ``_maybe_rerun_linker``).
* ``MIN_EXPERIMENTAL_INITIAL_PRIO_BUDGET`` gating in ``_get_next_batch``.
* Contextvar-plumbed scout budget via ``set_experimental_scout_budget``.
* ``ExperimentalSubquestionScore`` scoring + ``ExperimentalScoringCompletedEvent``.
"""

from rumil.prioritisers.question_prioritiser import QuestionPrioritiser


class ExperimentalQuestionPrioritiser(QuestionPrioritiser):
    summarise_before_assess: bool = False
