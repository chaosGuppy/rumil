## the task

you're doing a **scout subquestions** call — an initial exploration
of a question. your job is to identify **subquestions whose answers
would most advance understanding of the parent question**, and to
produce a few initial considerations that bear on it.

this is early-stage scouting: you're planting stakes that later
investigation can refine, refute, or build on. you are *not* trying
to answer the parent question definitively.

## a few moves

before producing subquestions, name the cached take. what's the
obvious decomposition a sharp person in this space would reach for?
write it down. is it actually the most informative way to carve the
question, or is it just the most familiar? sometimes the cached
decomposition is right; sometimes the question wants a different
axis (mechanism vs timeline, descriptive vs prescriptive, near-term
vs structural).

if the question is subtly malformed or compresses things that should
come apart, flag that — sometimes the most useful move is creating
a sub-question that surfaces the framing problem.

## what to produce

1. **2-4 subquestions.** each should decompose the parent into a
   piece that, if answered well, would substantially advance
   understanding of the whole. avoid subquestions that just restate
   the parent in different words, or address marginal aspects. think
   about what you would *most want to know* if you were trying to
   answer the parent — those are your subquestions.

2. **1-3 initial considerations.** claims that bear directly on the
   parent question. these may be tentative — the point is to plant
   stakes that later investigation can refine. where you have a
   provisional answer to one of the subquestions you're posing, state
   it as a claim with appropriately low credence and robustness; the
   reasoning fields are where you say what would firm it up.

## how to proceed

1. read the parent question and existing context carefully.
2. identify the most informative axes of decomposition — the
   subquestions whose answers would do the most to resolve the parent.
3. for each subquestion, use `create_question`. it's automatically
   linked as a child of the parent.
4. where you can offer even a tentative answer or relevant
   consideration, use `create_claim` and `link_consideration` to
   attach it to the parent (or to a subquestion if it bears more
   directly there).

## quality bar

- **informative decomposition over exhaustive coverage.** two
  subquestions that cut to the heart of the matter beat five that
  nibble at the edges.
- **subquestions should be substantially independent.** if answering
  one would largely answer another, merge them or drop the weaker.
- **tentative claims are valuable.** a provisional answer with
  robustness 1-2 gives later calls something concrete to evaluate.
  don't shy away from stating a view just because you're uncertain —
  flag the uncertainty in the scores, and explain where the
  uncertainty sits in their reasoning fields.
- **don't duplicate** subquestions or considerations already in the
  workspace.
