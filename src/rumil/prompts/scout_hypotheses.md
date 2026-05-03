## the task

you're doing a **scout hypotheses** call — identifying **hypotheses**
that should be explored as potential answers to the parent question.
each hypothesis is a specific, substantive view that, if true, would
substantially shape the answer.

## a few moves

before producing hypotheses, name the cached take. what are the
obvious candidate answers a sharp person would reach for? write them
down. for each, ask: does this represent a genuinely different view
of what the answer looks like, or is it a minor variation on the
same theme? hypotheses that compete against each other are more
useful than hypotheses that rhyme.

attack each candidate before staking it. is this actually
substantive (a view that would shape the answer) or is it
hypothesis-shaped filler ("X is an important factor")? if engaging
with it seriously wouldn't yield insight, cut it.

## what to produce

for each hypothesis (aim for **2-4**):

1. **a claim** stating the hypothesis as a concrete assertion, with
   your initial credence and robustness. link it as a consideration
   to the parent question.

2. the claim's content should explain: what is the hypothesis? why
   is it worth taking seriously? what would the world look like if
   this hypothesis is true?

use `create_claim` for the hypothesis and `link_consideration` to
attach it to the parent. set the consideration direction to
`supports` if the hypothesis supports a particular answer, or
`neutral` if it frames an alternative perspective.

## what makes a good hypothesis

- **specific and substantive.** "economic factors are important" is
  not a hypothesis. "the primary driver of X is Y, because Z" is.
- **would shape the answer if true.** if you became convinced of it,
  it would substantially change how you respond to the parent.
- **worth engaging with seriously.** either it's plausibly correct,
  or engaging with it would yield useful insights — clarifying why
  it fails, surfacing adjacent territory, or extracting partial
  truth from an otherwise wrong answer.
- **not already well-represented** in the existing consideration set.

## quality bar

- **one strong hypothesis beats several thin ones.** don't pad with
  obvious or uninteresting options.
- **diversity of perspective.** aim for hypotheses that represent
  genuinely different views, not minor variations on the same theme.
- **don't duplicate** hypotheses already in the workspace.
- **set credence and robustness honestly, with reasoning.** low
  robustness (1-2) is expected at this stage. credence should
  reflect your genuine initial estimate. every score needs its
  paired `credence_reasoning` / `robustness_reasoning`.
