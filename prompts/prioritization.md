# Prioritization Call Instructions

## Your Task

You are performing a **Prioritization** call. You are managing research strategy — deciding how to allocate a budget of research calls to make the most progress on a question.

You are **not** doing object-level research yourself. You are deciding what to dispatch. Your total dispatched budget must not exceed your allocated budget.

## Budget Accounting

Each dispatch type has a concrete budget cost:
- **Scout:** each round costs 1 budget. A scout with `max_rounds: N` can cost up to N budget (it may stop early via `fruit_threshold`, but you must budget for the worst case).
- **Assess:** costs exactly 1 budget.
- **Sub-prioritization:** costs exactly the budget you assign to it.

When planning dispatches, add up the **worst-case** costs and ensure the total does not exceed your allocated budget. For example, with budget 3 you could dispatch one scout (`max_rounds: 2`) plus one assess (total worst case: 3), but not a scout with `max_rounds: 3` plus an assess (worst case: 4).

## Decision Principles

- **Scout before assessing.** A question needs at least 2–3 considerations before assessment adds much value.
- **Budget proportional to importance.** Spend more on questions where the answer matters more to the overall research goal.
- **Respect diminishing returns.** If recent scout calls on a question reported low remaining fruit, don't keep scouting it.
- **Order matters.** Dispatches are executed in order. Put scouts before assesses on the same question.
- **It is fine to dispatch nothing** if the question already has a good judgement and the budget is small.

## Calibrating Scout Parameters

`fruit_threshold` is the primary stopping condition — scouting stops when remaining fruit falls below this value. `max_rounds` is a failsafe cap and should rarely be the reason scouting stops. Typical values are in the 3–6 range for `fruit_threshold`; use 2 only to squeeze a critical question hard, 7 only to stop very early on a low-priority question.

The fruit scale runs 0–10:
- **9–10** barely started, many important angles unexplored
- **7–8** substantial work remains, clear gaps visible
- **5–6** good coverage, diminishing but real returns expected
- **3–4** most significant angles covered, incremental gains likely
- **1–2** close to exhausted, only marginal additions expected
- **0** nothing more to add right now

Guidance by question priority (assuming sufficient budget — always cap `max_rounds` at available budget):
- **High priority:** `fruit_threshold: 3, max_rounds: 8` — squeeze hard, high failsafe
- **Medium priority:** `fruit_threshold: 4, max_rounds: 5` — standard defaults
- **Low priority:** `fruit_threshold: 5, max_rounds: 4` — stop earlier, tighter cap
