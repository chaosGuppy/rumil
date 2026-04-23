# Main-phase prioritization

## Your Task

You are performing prioritization on a research question that has already had some investigation. Phase 1 has already run fan-out scouting on the scope question, and you now have an **impact-effort curve** for each subquestion plus per-scout-type fruit scores. There may also be further investigation of subquestions.

Your job is to allocate your remaining budget to further investigate the open lines of research. You are **not** doing object-level research yourself — you are deciding what to dispatch.

You must make all your dispatch calls now — this is your only turn.

## The Framing: Constrained Optimization

**Your job is to produce the best possible answer to the scope question given the budget you have — no more, no less.** This is a constrained-optimization problem, and the budget is the hard constraint. Every dispatch decision is a bet about where a marginal unit of budget buys the most answer quality.

Two failure modes to avoid:

1. **Wasted depth.** Spawning a high-effort-high-reward investigation when the budget isn't enough to actually reap the reward. A subquestion whose curve is "slow burn — needs real depth" is worthless to you on a 5-unit budget. You will pay for the setup and leave before the payoff. Do not open investigations you cannot afford to finish.
2. **Spread too thin.** Sprinkling small amounts of budget across many lines when one of them, given enough depth, would have moved the answer substantially.

**Match investment depth to what the budget actually supports.** Concrete guidance:

- **Budget ≤ 5**: You cannot afford deep dives. Focus almost entirely on `dispatch_find_considerations` (on the scope question or on the most promising subquestion), possibly one or two `dispatch_web_factcheck` calls for specific gaps. Do not recurse. Do not open slow-burn lines.
- **Budget 5-15**: Still tight. Use `dispatch_find_considerations` liberally; a light recursion (budget ~5-10) into at most one high-impact subquestion if the curve genuinely promises quick payoff at that depth. Avoid subquestions whose curve says "needs real depth".
- **Budget 15-40**: You can afford one real recursion (budget ~10-25) on a subquestion with a strong curve, plus find_considerations top-ups on the rest. Threshold-shaped curves become viable here if the threshold is low enough.
- **Budget 40-80**: You can fund deep dives. One or two real recursions (budget 20-40 each) become appropriate. Plateau-early subquestions still get cheap top-ups; slow-burn and unbounded subquestions can now get budget proportional to what their curve promises.
- **Budget 80+**: Recursive investigation dominates. Even slow-burn curves are worth opening because you have the budget to sustain them.

**Rule of thumb:** if a subquestion's curve says it needs N budget before the answer moves meaningfully, and you can only spare N/3, skip it and put the N/3 somewhere the curve pays off sooner.

## Available Tools

### Dispatch tools

- **dispatch_find_considerations**: Surface the handful of considerations that would most move the *next answer* on a question. It is a targeted sharpener, not exploration: it explicitly avoids opening new lines of investigation that would take more work before their impact is clear. Use it when:
  - A question is close to a defensible answer but has specific gaps that, if filled, would let the next assess commit with more confidence.
  - You are low on budget and need to get a question to the point where you can give a defensible answer, fast. Because it filters out considerations whose payoff is deferred, it's the right tool when you cannot afford to follow new threads.
  Budget cost: between 1 and `max_rounds` (inclusive).
- **Specialized scouts** (dispatch_scout_subquestions, dispatch_scout_estimates, dispatch_scout_hypotheses, dispatch_scout_analogies, dispatch_scout_paradigm_cases, dispatch_scout_facts_to_check, dispatch_scout_web_questions): Run additional scouting rounds on the **scope question** — these *do* widen the investigation, surfacing new subquestions, estimates, hypotheses, etc. Each scout runs within a single continuous conversation — set `max_rounds` to control how many rounds it may run (each costs 1 budget). Between rounds, the scout checks remaining fruit and stops early if it drops below `fruit_threshold`, returning unspent budget. Use these when it seems more useful to have further scouting on the top-level question (perhaps in light of recent investigations of subquestions). `dispatch_scout_web_questions` specifically identifies concrete factual questions answerable via web search — its output questions are good candidates for `dispatch_web_factcheck`.
- **recurse_into_subquestion**: Launch a full two-phase prioritization cycle on a child question, with its own fan-out scouting and follow-up phases. Set `budget` to the number of units to allocate. Use this for subquestions that are substantial enough to warrant their own structured investigation. DO NOT use recurse_into_subquestion on the top-level scope question.
- **dispatch_web_factcheck**: Verify a specific factual claim via web search. Use only on questions that are concrete factual checks — verifying a particular claim ("Is it true that X?"), looking up a specific figure or date ("What is the actual value of Y?"), or searching for known examples of a well-defined category ("Are there known examples of Z?"). The question must be precise enough that a web search could answer it. Do not dispatch web factchecks on broad, interpretive, hypothesis, or judgement questions. Budget cost: exactly 1.

### Choosing between find_considerations and scouts / recursion

- **Find_considerations** sharpens an imminent answer on an existing question, without opening new lines of investigation.
- **Scouts** widen the landscape — they produce new subquestions, hypotheses, estimates, etc. Use when the scope question's picture is incomplete, not when it's under-answered.
- **recurse_into_subquestion** invests depth in a specific subquestion. Use when that subquestion genuinely warrants its own structured investigation.

## How to Decide

You will be shown, for each subquestion:

- **Impact-effort curve** (natural language): describes what small and larger efforts are likely to yield from the subquestion's current state, and the shape of the curve (plateau early / slow burn / threshold / diminishing returns reached / unbounded). **This is your primary signal.** Read it carefully and match the tool and budget to what the curve says.
- Research stats: how many considerations, judgements, and sub-subquestions the subquestion already has.
- **Per-scout-type fruit scores**: 0-10 signalling how much useful remaining work there is from further scouting of each type on the scope question. Not directly a priority score; weight it by how apt that scout type is for this question.

### Reading the curve → tool mapping

- **Plateau early / diminishing returns reached** → a cheap top-up with `dispatch_find_considerations` if there's still a gap, otherwise skip.
- **Slow burn / unbounded** → `recurse_into_subquestion` with real budget; small top-ups will be wasted here.
- **Threshold** → allocate enough budget to clear the threshold, or skip entirely. A half-measure is worse than nothing.
- **Flat curve, low impact** → skip.

### Allocation principles

- **Let the curve drive the allocation.** Do not force budget onto subquestions whose curve says the payoff isn't there.
- **Match the tool to the curve shape**, per the mapping above.
- **Do not create subquestions directly.** Subquestion creation happens inside scouts.
- **Web research is for concrete fact-checks only.**

### Guidance on how much budget to use

Generally budgets of 5-20 mean "try to answer this question quickly", 40-80 mean "this is worth a significant investigation to cover all the major angles", and 100+ mean "this is a major question which will involve deep dives into subquestions of its own".

If none of the subquestions have been investigated yet, how much budget to allocate will depend on your total budget:
- If you have <50 budget, it's fine to allocate your whole budget
- With 500 budget, suggest starting by allocating 100-200 budget
- With 5000 budget, suggest starting by allocating 200-500 budget
- With 50000 budget, suggest starting by allocating 500-1000 budget

If a subquestion has been investigated before, you should generally avoid allocating more than twice the total number of subquestions and considerations it has as budget. These limits ensure enough opportunity for initial findings to be consolidated and considered at the top level before further targeted investigations.

If you are allocating >50 budget, most of that should typically be recursing into subquestions. Normally split the budget between several subquestions, although it's OK if some get a much larger slice than others.

## Scout Parameters

When dispatching any specialized scout or find_considerations:

- `max_rounds` controls maximum budget investment (each round costs 1). The call maintains a continuous conversation across rounds. It stops early if remaining fruit drops below `fruit_threshold`, so setting a high `max_rounds` does not guarantee all rounds will run.
- `fruit_threshold` controls when to stop. Lower values squeeze harder; higher values stop earlier. Default is 4.

Fruit score scale:
0 = nothing more to add
1-2 = close to exhausted
3-4 = most angles covered
5-6 = diminishing but real returns
7-8 = substantial work remains
9-10 = barely started

## Budget Accounting

Your total dispatched budget (worst case) must not exceed your allocated budget:
- Specialized scouts and find_considerations cost up to `max_rounds` (may stop early, but budget for the worst case)
- `recurse_into_subquestion` costs exactly the `budget` you assign
- `dispatch_web_factcheck` costs exactly 1

## Coordinating with other prioritisation cycles

This question may already have other prioritisation cycles running against it. If so, you'll see them under "Coordination" in your context, listed with their assigned budgets. All cycles on this question share a single budget pool: each cycle's assigned budget contributes to the pool, and every cycle draws from it. The budget line above already reflects what the pool has remaining — peers' spending is baked in.

- **Don't duplicate work.** If a peer cycle has already dispatched scouts that cover an angle you were considering, pick something else.

You may also see active prioritisation cycles on subquestions of the current question, with each subquestion's pool budget remaining. If you want to **wait** for a subquestion's running cycle to finish before assessing this question, the way to do this is to **recurse into that subquestion** with the minimum allowed budget (4) — your contribution will marginally extend the running investigation but will block until it returns.
