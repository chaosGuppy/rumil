## the task

you're doing a **scout cruxes** call. your job is to identify
specific points where the how-true and how-false stories for the
scope claim diverge — points such that resolving them would tell
you which story is closer to the truth.

a crux is a point of disagreement between stories that is both
**important** (resolving it would shift your view significantly)
and **tractable** (it's feasible to investigate).

## a few moves

before producing cruxes, read the existing how-true and how-false
stories carefully. name the cached cruxes — the ones a sharp person
would reach for on autopilot. write them down. for each, ask: do
the two stories *actually* diverge here, or am i pattern-matching
on a generic "this is the kind of thing that's contested" feeling?
a real crux makes specific, different predictions under the two
stories.

if the cached cruxes are exactly right, fine — stake them. if they
miss the actually-load-bearing disagreement, find your way to the
better cruxes by working through where the two stories make
contradictory predictions.

## what to produce

aim for **2-4 cruxes**. for each:

1. **a claim** if the crux is an assertion whose truth is
   load-bearing ("the rate of X is above Y", "mechanism Z is the
   primary driver"). use `create_claim`, link with
   `link_consideration` to the scope claim.

2. **a question** if the crux is something whose answer would
   discriminate between stories ("what is the actual rate of X?",
   "does mechanism Z operate in context W?"). use `create_question`
   — it auto-links to the scope claim.

for each, the content should explain: what exactly is the
disagreement? what would you expect to observe under each story?
how feasible is it to investigate?

## quality bar

- **discriminating power.** a real crux is one where the how-true
  and how-false stories predict different things. a point they agree
  on is not a crux.
- **specificity.** "whether the evidence is reliable" is too vague.
  "whether study X's sample was representative of population Y" is
  a crux.
- **tractability.** prioritise cruxes that could actually be
  investigated over ones that are important but irresolvable.
- **don't duplicate** cruxes already in the workspace.
