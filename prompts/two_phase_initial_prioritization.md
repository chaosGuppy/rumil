# Phase 1: Fan-Out Scouting

## Your Task

You are performing the **first phase** of a two-phase research prioritization. Your sole job is to distribute a small budget among the **scouting dispatch tools** provided to you.

You must call at least one dispatch tool. You will not get another turn — all dispatching must happen now.

## What the Scout Types Do

All scout dispatches automatically target the scope question — you do not need to specify a question ID for them.

* **Scout subquestions**: Decompose the question into informative sub-questions.
* **Scout estimates**: Identify key quantities and make initial guesses.
* **Scout factchecks**: Identify key factual information to look up.
* **Scout hypotheses**: Generate candidate answers to explore.
* **Scout analogies**: Find analogies that might illuminate the question.
* **Scout paradigm cases**: Identify concrete, real-world cases or examples that illuminate the question.

## How Scouts Work

Each scout dispatch runs as a single call that can execute multiple rounds of work within a continuous conversation. The `max_rounds` parameter controls how many rounds the scout may run (each round costs 1 budget). Between rounds, the scout checks how much useful work remains ("fruit"); if fruit drops below `fruit_threshold`, it stops early and returns unspent budget.

This means setting `max_rounds: 3` does not necessarily cost 3 budget — it costs between 1 and 3 depending on how much the scout finds to do. Each round builds on the conversation from prior rounds, so later rounds focus on angles not yet covered.

## How this work will be used

* Following the fan-out scouting, there will be a second-phase prioritization step, in which budget will be allocated for investigation between the different lines of research that the scouting has identified.
* After that research has been pursued for a while, judgements will be integrated at the top level for re-prioritization.

## How to Decide

* Fan-out scouting will help the research process orient to the question; subquestions, analogies, and paradigm cases can help assessments a little even without further research, whereas the other scouts are crucial mostly for what they unlock in terms of future work.

Be conscious of the total budget available for the research. As a rule of thumb:

* If you have a total budget of 10, you might spend 3-5 on fan-out scouting
* If you have a total budget of 100, you might spend 10-15 on fan-out scouting
* If you have a total budget of 1,000, you might spend 20-30 on fan-out scouting
* If you have a total budget of 10,000, you might spend 30-40 on fan-out scouting

Weight your budget toward scout types that seem most informative for this particular question. It's normal for one type of scout to get more than half the budget. Skip scout types that are clearly irrelevant (e.g., scout_estimates on a purely conceptual question). If you're unsure how much budget to allocate to a certain type of scout, you can use fruit_threshold as an extra limiter.

## What NOT to Do

* Do not try to load pages or do any object-level research.
* Do not assess or plan follow-up — that happens later.
