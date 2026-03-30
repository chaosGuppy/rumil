Phase 1: Fan-Out Scouting (Claim Investigation)

Your Task

You are performing the first phase of a two-phase research prioritization for investigating a claim. Your sole job is to distribute a small budget among the scouting dispatch tools provided to you.


You must call at least one dispatch tool. You will not get another turn — all dispatching must happen now.


What the Scout Types Do

All scout dispatches automatically target the scope claim — you do not need to specify a claim ID for them.



scout_c_how_true: Identify plausible causal mechanisms that would make the claim true. Produces claims (e.g. "mechanism X could explain why this is true").

scout_c_how_false: Identify plausible causal stories compatible with observed evidence but in which the claim is false. Produces claims (e.g. "mechanism Y would explain the same observations but the claim would be false").

scout_c_cruxes: Identify specific points where the "how true" and "how false" stories diverge, such that resolving them would be most informative. Produces claims or questions depending on the nature of the crux. Requires at least one how-true and one how-false story to already exist.

scout_c_relevant_evidence: Identify evidence worth gathering that bears on the most important cruxes. Produces questions (e.g. "what does the empirical literature say about X?" or "what is the actual rate of Y?"). Most useful after cruxes have been identified.

scout_c_stress_test_cases: Identify concrete scenarios that could serve as hard tests for the claim, especially boundary cases where competing stories predict different outcomes. Produces questions (e.g. "what does scenario S tell us about the claim?").


How Scouts Work

Each scout dispatch runs as a single call that can execute multiple rounds of work within a continuous conversation. The max_rounds parameter controls how many rounds the scout may run (each round costs 1 budget). Between rounds, the scout checks how much useful work remains ("fruit"); if fruit drops below fruit_threshold, it stops early and returns unspent budget.


This means setting max_rounds: 3 does not necessarily cost 3 budget — it costs between 1 and 3 depending on how much the scout finds to do. Each round builds on the conversation from prior rounds, so later rounds focus on angles not yet covered.


How this work will be used


Following the fan-out scouting, there will be a second-phase prioritization step, in which budget will be allocated for deeper investigation of the most promising lines of inquiry.

After that research has been pursued for a while, the claim's credence and robustness scores will be reassessed.


How to Decide


The natural starting point is usually scout_c_how_true and scout_c_how_false, since the other scouts build on having competing causal stories in place.

scout_c_cruxes and scout_c_relevant_evidence are most valuable after the how-true and how-false stories exist, but can still be dispatched in the first phase if budget allows.

scout_c_stress_test_cases can be dispatched at any point — it doesn't strictly require the other scouts to have run first, though it benefits from having competing stories to test between.


Be conscious of the total budget available for the research. As a rule of thumb:



If you have a total budget of 10, you might spend 3-5 on fan-out scouting

If you have a total budget of 100, you might spend 10-15 on fan-out scouting

If you have a total budget of 1,000, you might spend 20-30 on fan-out scouting

If you have a total budget of 10,000, you might spend 30-40 on fan-out scouting


Weight your budget toward scout types that seem most informative for this particular claim. It's normal for one type of scout to get more than half the budget. Skip scout types that are clearly irrelevant.


What NOT to Do


Do not try to load pages or do any object-level research.

Do not assess or plan follow-up — that happens later.
