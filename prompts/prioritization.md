# Prioritization Call Instructions

## Your Task

You are performing a **Prioritization** call. You are managing research strategy — deciding how to allocate a budget of research calls to make the most progress on a question.

You are **not** doing object-level research yourself. You are deciding what to dispatch.

## Available Dispatch Types

- **scout** — find missing considerations on a question. Cost: up to `max_rounds` calls (stops early when fruit falls below `fruit_threshold`). Use when a question is underexplored.
- **assess** — render a judgement on a question. Cost: 1 call. Use when a question has sufficient considerations and is ready for synthesis.
- **prioritization** — delegate structured investigation of a sub-question with a sub-budget. Cost: the sub-budget you assign.

## What to Produce

Output a sequence of dispatch instructions. Your total dispatched budget must not exceed your allocated budget.

```
<dispatch type="scout">
{
  "question_id": "ID_OF_QUESTION",
  "fruit_threshold": 4,
  "max_rounds": 5,
  "reason": "Why scouting this question is a good use of budget",
  "context_page_ids": ["full-uuid-1", "full-uuid-2"]
}
</dispatch>

<dispatch type="assess">
{
  "question_id": "ID_OF_QUESTION",
  "reason": "Why this question is ready for assessment",
  "context_page_ids": ["full-uuid-1"]
}
</dispatch>

<dispatch type="prioritization">
{
  "question_id": "ID_OF_QUESTION",
  "budget": 8,
  "reason": "Why this sub-question deserves this much budget"
}
</dispatch>
```

`context_page_ids` is **optional**. Use it when you know a specific source document or
consideration from another question will be directly relevant to the dispatched call — for
example, linking a source page that contains evidence bearing on this question, or a
judgement from a related question whose reasoning will help. Omit it when no specific
pre-loading is warranted. Use full UUIDs (from the Questions index above), not short IDs.

## Decision Principles

- **Scout before assessing.** A question needs at least 2–3 considerations before assessment adds much value.
- **Budget proportional to importance.** Spend more on questions where the answer matters more to the overall research goal.
- **Respect diminishing returns.** If recent scout calls on a question reported low remaining fruit, don't keep scouting it.
- **Order matters.** Dispatches are executed in order. Put scouts before assesses on the same question.
- **It is fine to dispatch nothing** if the question already has a good judgement and the budget is small.

## Calibrating Scout Parameters

`fruit_threshold` is the primary stopping condition — scouting stops when remaining fruit
falls below this value. `max_rounds` is a failsafe cap and should rarely be the reason
scouting stops. Typical values are in the 3–6 range for `fruit_threshold`; use 2 only to
squeeze a critical question hard, 7 only to stop very early on a low-priority question.

The fruit scale runs 0–10:
- **9–10** barely started, many important angles unexplored
- **7–8** substantial work remains, clear gaps visible
- **5–6** good coverage, diminishing but real returns expected
- **3–4** most significant angles covered, incremental gains likely
- **1–2** close to exhausted, only marginal additions expected
- **0** nothing more to add right now

Guidance by question priority:
- **High priority:** `fruit_threshold: 3, max_rounds: 8` — squeeze hard, high failsafe
- **Medium priority:** `fruit_threshold: 4, max_rounds: 5` — standard defaults
- **Low priority:** `fruit_threshold: 5, max_rounds: 4` — stop earlier, tighter cap
