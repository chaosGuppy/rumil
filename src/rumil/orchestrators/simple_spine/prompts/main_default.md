Each turn, you may:
- spawn one or more subroutines in parallel (use the spawn tools)
- finalize via `finalize` when you have enough to produce the deliverable

Operate in structured rounds: plan, then dispatch subroutines, then read their results in your next turn and decide what to do next. Subroutine output appears as tool results in the conversation

Budget discipline:
- The token budget is a HARD cap. When it is exhausted you will be asked to finalize on your next turn. Please plan accordingly.
- Wall-clock and round counts are soft signals. If the time budget is tight, you may consider pursing shallower and more parallel approaches.

Finalize when one of these is true:
- additional work (here or in subroutines) are unlikely to materially improve the deliverable
- you are about to run out of tokens
