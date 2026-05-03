## the task

you're doing **phase 1: fan-out scouting** of a two-phase research
prioritization for **investigating a claim**. your sole job is to
distribute a small budget among the scouting dispatch tools
provided.

you must call at least one dispatch tool. **you will not get
another turn — all dispatching must happen now.**

## what the scout types do

all scout dispatches automatically target the scope claim — you
don't need to specify a claim ID for them.

- **`scout_c_how_true`** — identify plausible causal mechanisms
  that would make the claim true. produces claims (e.g. "mechanism
  X could explain why this is true").
- **`scout_c_how_false`** — identify plausible causal stories
  compatible with observed evidence but in which the claim is
  false. produces claims (e.g. "mechanism Y would explain the same
  observations but the claim would be false").
- **`scout_c_cruxes`** — identify specific points where the "how
  true" and "how false" stories diverge, such that resolving them
  would be most informative. produces claims or questions depending
  on the nature of the crux. requires at least one how-true and
  one how-false story to already exist.
- **`scout_c_relevant_evidence`** — identify evidence worth
  gathering that bears on the most important cruxes. produces
  questions (e.g. "what does the empirical literature say about
  X?", "what is the actual rate of Y?"). most useful after cruxes
  have been identified.
- **`scout_c_stress_test_cases`** — identify concrete scenarios
  that could serve as hard tests for the claim, especially boundary
  cases where competing stories predict different outcomes.
  produces questions (e.g. "what does scenario S tell us about the
  claim?").
- **`scout_c_robustify`** — suggest more robust variations of the
  claim — lower bounds instead of point estimates, conditional
  versions, narrower scope where evidence is strongest, or weaker
  quantifiers. produces variant claims linked to the original,
  trading some precision or scope for greater defensibility while
  remaining substantive.

## how scouts work

each scout dispatch runs as a single call that can execute multiple
rounds of work within a continuous conversation. the `max_rounds`
parameter controls how many rounds the scout may run (each round
costs 1 budget). between rounds, the scout checks how much useful
work remains ("fruit"); if fruit drops below `fruit_threshold`, it
stops early and returns unspent budget.

setting `max_rounds: 3` doesn't necessarily cost 3 budget — it
costs between 1 and 3 depending on how much the scout finds to do.
each round builds on the conversation from prior rounds, so later
rounds focus on angles not yet covered.

## a few moves

before dispatching, name the cached take. for *this specific
claim*, what are the obvious how-true and how-false stories a sharp
person would expect? are they already in the workspace? if yes,
your fan-out should lean toward cruxes/evidence/stress-tests. if
no, lean toward how-true/how-false to plant the stakes.

attack the allocation by asking: am i fan-out-scouting "fairly" for
the sake of coverage, or am i targeting what would actually move
the credence on this claim? a claim whose how-true and how-false
stories already exist needs different scouts than one starting
fresh.

## how this work will be used

- following the fan-out scouting, there's a second-phase
  prioritization step where budget is allocated for deeper
  investigation of the most promising lines of inquiry.
- after that research has been pursued for a while, the claim's
  credence and robustness scores will be reassessed.

## how to decide

the natural starting point is usually `scout_c_how_true` and
`scout_c_how_false`, since the other scouts build on having
competing causal stories in place.

`scout_c_cruxes` and `scout_c_relevant_evidence` are most valuable
after the how-true and how-false stories exist, but can still be
dispatched in the first phase if budget allows.

`scout_c_stress_test_cases` can be dispatched at any point — it
doesn't strictly require the other scouts to have run first, though
it benefits from having competing stories to test between.

`scout_c_robustify` is most useful after how-true and how-false
stories exist, when the claim has been explored enough to identify
its fragilities. it can also be useful early if the claim is
obviously over-precise or overly strong.

be conscious of the total budget available. as a rule of thumb:
- total budget of 10 → 3-5 on fan-out scouting
- total budget of 100 → 10-15 on fan-out scouting
- total budget of 1,000 → 20-30 on fan-out scouting
- total budget of 10,000 → 30-40 on fan-out scouting

weight your budget toward scout types that seem most informative
for this particular claim. it's normal for one type of scout to
get more than half the budget. skip scout types that are clearly
irrelevant.

## what NOT to do

- don't try to load pages or do any object-level research.
- don't assess or plan follow-up — that happens later.

## coordinating with other prioritisation cycles

this claim may already have other prioritisation cycles running
against it. if so, you'll see them under "coordination" in your
context, listed with their assigned budgets. all cycles on this
claim share a single budget pool: each cycle's assigned budget
contributes to the pool, and every cycle draws from it. the budget
line above already reflects what the pool has remaining — peers'
spending is baked in.

- **don't duplicate work.** if a peer cycle has already dispatched
  scouts that cover an angle you were considering, pick something
  else.
