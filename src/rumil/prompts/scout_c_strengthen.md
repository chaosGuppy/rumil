## the task

you're doing a **scout strengthen** call. the scope claim already
has high credence — it's believed to be robustly true. your job is
to suggest variations that are **more precise, specific, or
stronger** while maintaining that high credence.

this is the opposite of robustification: instead of weakening for
safety, you're tightening for informativeness.

## a few moves

before producing variants, look at the existing how-false stories
and assessment results. where is the claim weaker than it needs to
be given the evidence? a stronger version says *more* — narrower
error bars, tighter bounds, removed hedges, added conjuncts that
the evidence supports.

attack each candidate by asking: does this maintain high credence,
or am i over-tightening? if you can't maintain credence ≥8 after
strengthening, the variant is too aggressive — back off.

## what to produce

for each strengthened variation (aim for **1 or 2**):

1. **a claim** describing the stronger version. use `create_claim`
   to create the variant, then `link_variant` to link it back to
   the original scope claim.

2. in the claim body, explain what was strengthened and why credence
   should remain high.

## strengthening strategies

(use whichever are appropriate):

- **add quantitative bounds.** "X increases Y by at least Z%"
  instead of "X increases Y."
- **narrow error bars.** if the original says "at least 30%", can
  the evidence support "at least 40%"?
- **strengthen quantifiers.** "nearly all Fs are G" instead of
  "most Fs are G", if the evidence supports it.
- **add specificity.** name specific sub-domains or conditions
  where the claim holds even more strongly.
- **remove unnecessary hedges.** if a conditional ("if A, then X")
  has A well-established, state X directly.
- **add a conjunct.** if A and B are well-supported and C is also
  well-supported, assert A and B and C.

## how to proceed

1. read the scope claim and existing context carefully — especially
   the how-false stories and assessment results.
2. identify where the claim is weaker than it needs to be given the
   evidence.
3. for each weakness, suggest a variation that strengthens the
   claim.
4. after creating each variation, use `link_variant` to link the new
   claim to the scope claim.
5. set credence at least as high as the original — if you can't
   maintain high credence, the strengthening is too aggressive.
6. set robustness at least as high as the original.
7. pair every score with its reasoning field. in
   `credence_reasoning`, explain why the stronger wording is still
   supported; in `robustness_reasoning`, say what would further firm
   it up or what could still move it.

## quality bar

- **maintain credence.** if strengthening drops credence below 8,
  you've gone too far.
- **genuinely stronger.** the variation should say more, not just
  rephrase. more precision, more specificity, tighter bounds.
- **evidence-based.** only strengthen where the existing research
  and context support it. don't speculate.
- **don't duplicate** variations already in the workspace.
