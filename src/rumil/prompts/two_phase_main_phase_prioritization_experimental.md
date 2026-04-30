## the task

you're doing **main-phase prioritization** (experimental variant)
on a research question that has already had some investigation.
phase 1 has already run fan-out scouting on the scope question, and
you now have an **impact-effort curve** for each sub-question plus
per-scout-type fruit scores. there may also be further investigation
of sub-questions.

your job: allocate your remaining budget to further investigate the
open lines of research. you are **not** doing object-level research
yourself — you're deciding what to dispatch.

**you must make all your dispatch calls now — this is your only
turn.**

## the framing: constrained optimization

**your job is to produce the best possible answer to the scope
question given the budget you have — no more, no less.** this is a
constrained-optimization problem, and the budget is the hard
constraint. every dispatch decision is a bet about where a marginal
unit of budget buys the most answer quality.

two failure modes to avoid:

1. **wasted depth.** spawning a high-effort-high-reward
   investigation when the budget isn't enough to actually reap the
   reward. a sub-question whose curve is "slow burn — needs real
   depth" is worthless to you on a 5-unit budget. you'll pay for
   the setup and leave before the payoff. don't open investigations
   you can't afford to finish.
2. **spread too thin.** sprinkling small amounts of budget across
   many lines when one of them, given enough depth, would have
   moved the answer substantially.

**match investment depth to what the budget actually supports.**
concrete guidance:

- **budget ≤ 5:** can't afford deep dives. focus almost entirely
  on `dispatch_find_considerations` (on the scope question or on
  the most promising sub-question), possibly one or two
  `dispatch_web_factcheck` calls for specific gaps. don't recurse.
  don't open slow-burn lines.
- **budget 5-15:** still tight. use `dispatch_find_considerations`
  liberally; a light recursion (budget ~5-10) into at most one
  high-impact sub-question if the curve genuinely promises quick
  payoff at that depth. avoid sub-questions whose curve says
  "needs real depth".
- **budget 15-40:** can afford one real recursion (budget ~10-25)
  on a sub-question with a strong curve, plus find_considerations
  top-ups on the rest. threshold-shaped curves become viable here
  if the threshold is low enough.
- **budget 40-80:** can fund deep dives. one or two real
  recursions (budget 20-40 each) become appropriate. plateau-early
  sub-questions still get cheap top-ups; slow-burn and unbounded
  sub-questions can now get budget proportional to what their curve
  promises.
- **budget 80+:** recursive investigation dominates. even slow-burn
  curves are worth opening because you have the budget to sustain
  them.

**rule of thumb:** if a sub-question's curve says it needs N budget
before the answer moves meaningfully, and you can only spare N/3,
skip it and put the N/3 somewhere the curve pays off sooner.

## a few moves

before allocating, name the cached take. given the impact-effort
curves, what's the obvious allocation? write it down. now check it
against the budget-shape guidance above — am i actually matching
investment depth to what the budget supports, or am i giving in to
the "spread fairly" instinct?

attack the allocation by asking: for each candidate dispatch, what
does the curve actually say about the payoff at the depth i'm
funding? if the curve says "needs real depth" and i'm giving it
half-depth, the payoff isn't there. cut.

## available tools

### dispatch tools

- **`dispatch_find_considerations`** — surface the handful of
  considerations that would most move the *next answer* on a
  question. targeted sharpener, not exploration. use when:
  - a question is close to a defensible answer but has specific
    gaps that, if filled, would let the next assess commit with
    more confidence.
  - you're low on budget and need to get a question to the point
    where you can give a defensible answer, fast.
  budget cost: between 1 and `max_rounds` (inclusive).
- **specialized scouts** (`dispatch_scout_subquestions`,
  `dispatch_scout_estimates`, `dispatch_scout_hypotheses`,
  `dispatch_scout_analogies`, `dispatch_scout_paradigm_cases`,
  `dispatch_scout_facts_to_check`, `dispatch_scout_web_questions`)
  — run additional scouting rounds on the **scope question**,
  widening the investigation. each runs within a single continuous
  conversation; `max_rounds` controls rounds (each costs 1). stops
  early if fruit drops below `fruit_threshold`.
- **`recurse_into_subquestion`** — launch a full two-phase
  prioritization cycle on a child question. set `budget` to the
  units to allocate. **don't use on the top-level scope question.**
- **`dispatch_web_factcheck`** — verify a specific factual claim
  via web search. use only on questions that are concrete factual
  checks. budget cost: exactly 1.

### choosing between find_considerations and scouts / recursion

- **find_considerations** sharpens an imminent answer on an
  existing question, without opening new lines of investigation.
- **scouts** widen the landscape — produce new sub-questions,
  hypotheses, estimates, etc. use when the scope question's
  picture is incomplete, not when it's under-answered.
- **`recurse_into_subquestion`** invests depth in a specific
  sub-question. use when that sub-question genuinely warrants its
  own structured investigation.

## how to decide

you'll be shown, for each sub-question:

- **impact-effort curve** (natural language): describes what small
  and larger efforts are likely to yield from the sub-question's
  current state, and the shape of the curve (plateau early / slow
  burn / threshold / diminishing returns reached / unbounded).
  **this is your primary signal.** read it carefully and match the
  tool and budget to what the curve says.
- research stats: how many considerations, judgements, and
  sub-sub-questions the sub-question already has.
- **per-scout-type fruit scores**: 0-10 signalling how much useful
  remaining work there is from further scouting of each type on
  the scope question. weight it by how apt that scout type is for
  this question.

### reading the curve → tool mapping

- **plateau early / diminishing returns reached** → a cheap top-up
  with `dispatch_find_considerations` if there's still a gap,
  otherwise skip.
- **slow burn / unbounded** → `recurse_into_subquestion` with real
  budget; small top-ups will be wasted here.
- **threshold** → allocate enough budget to clear the threshold,
  or skip entirely. a half-measure is worse than nothing.
- **flat curve, low impact** → skip.

### allocation principles

- **let the curve drive the allocation.** don't force budget onto
  sub-questions whose curve says the payoff isn't there.
- **match the tool to the curve shape**, per the mapping above.
- **don't create sub-questions directly.** sub-question creation
  happens inside scouts.
- **web research is for concrete fact-checks only.**

### guidance on how much budget to use

generally budgets of 5-20 mean "try to answer this question
quickly", 40-80 mean "this is worth a significant investigation to
cover all the major angles", and 100+ mean "this is a major
question which will involve deep dives into sub-questions of its
own".

if none of the sub-questions have been investigated yet:
- <50 budget: fine to allocate your whole budget
- 500 budget: suggest starting by allocating 100-200
- 5000 budget: suggest starting by allocating 200-500
- 50000 budget: suggest starting by allocating 500-1000

if a sub-question has been investigated before, generally avoid
allocating more than twice the total number of sub-questions and
considerations it has as budget. these limits ensure enough
opportunity for initial findings to be consolidated.

if you're allocating >50 budget, most of that should typically be
recursing into sub-questions. split between several sub-questions,
although it's OK if some get a much larger slice than others.

## scout parameters

when dispatching any specialized scout or find_considerations:

- `max_rounds` controls maximum budget investment (each round costs
  1). the call maintains a continuous conversation across rounds.
  stops early if remaining fruit drops below `fruit_threshold`.
- `fruit_threshold` controls when to stop. lower values squeeze
  harder; higher values stop earlier. default is 4.

fruit score scale:
- 0 = nothing more to add
- 1-2 = close to exhausted
- 3-4 = most angles covered
- 5-6 = diminishing but real returns
- 7-8 = substantial work remains
- 9-10 = barely started

## budget accounting

your total dispatched budget (worst case) must not exceed your
allocated budget:
- specialized scouts and find_considerations cost up to
  `max_rounds`
- `recurse_into_subquestion` costs exactly the `budget` you assign
- `dispatch_web_factcheck` costs exactly 1

## coordinating with other prioritisation cycles

this question may already have other prioritisation cycles running
against it. if so, you'll see them under "coordination" in your
context, listed with their assigned budgets. all cycles on this
question share a single budget pool: each cycle's assigned budget
contributes to the pool, and every cycle draws from it. the budget
line above already reflects what the pool has remaining — peers'
spending is baked in.

- **don't duplicate work.** if a peer cycle has already dispatched
  scouts that cover an angle you were considering, pick something
  else.

you may also see active prioritisation cycles on sub-questions of
the current question, with each sub-question's pool budget
remaining. if you want to **wait** for a sub-question's running
cycle to finish before assessing this question, recurse into that
sub-question with the minimum allowed budget (4) — your
contribution will marginally extend the running investigation but
will block until it returns.
