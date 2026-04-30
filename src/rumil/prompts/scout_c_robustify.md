## the task

you're doing a **scout robustify** call — suggesting variations of
the scope claim that are more robustly true while remaining
substantive. the goal is to find a version that survives more
scenarios than the original, without becoming so weak it's not
useful.

## a few moves

before producing variants, identify what makes the claim fragile.
is it overly precise (a point estimate where a range would do)? too
universal (claiming "always" when "usually" would suffice)?
dependent on a specific uncertain assumption? making a strong
causal claim where only correlation is established? name the
fragility, then attack it.

watch for the failure mode where "more robust" becomes "more
vague." a well-supported lower bound is robustness; "something
might happen" is just hedging. the robust version must still bear
on research questions and decisions.

## what to produce

for each robust variation (aim for **1 or 2**):

1. **a claim** describing the more robust version. use
   `create_claim` to create the variant, then `link_variant` to
   link it back to the original scope claim. this preserves the
   original while recording the relationship.

2. in the claim body, explain briefly what makes this variation more
   robust and why it still has enough substance to be useful.

## robustification strategies

(use whichever are appropriate for the claim at hand):

- **lower bounds instead of point estimates.** "X is at least 30%"
  instead of "X is about 50%."
- **conditional claims.** "if assumption A holds, then X" is more
  robust than bare "X" when A is uncertain but plausible.
- **narrower scope.** "X holds in domain D (where evidence is
  strongest)" instead of "X holds everywhere."
- **weaker quantifiers.** "most Fs are G" instead of "all Fs are G."
- **dropping the weakest conjunct.** if the original asserts A and B
  and C, and C is the weakest link, assert just A and B.

## how to proceed

1. read the scope claim and existing context carefully.
2. identify what makes the claim fragile.
3. for each fragility, suggest a variation that removes or reduces
   it.
4. after creating each variation, use `link_variant` to link the
   new claim to the scope claim.
5. set robustness higher than the original — that's the whole
   point.
6. set credence at least as high as the original, usually higher (a
   weaker claim should be more credible).

## quality bar

- **still substantive.** "something might happen" is not useful.
  the robust version must say something specific enough to bear on
  research questions and decisions.
- **genuinely more robust.** the variation should survive more
  scenarios, not just be vaguer. a well-supported lower bound beats
  a vague hedge.
- **explain the trade-off.** note what precision or scope is being
  sacrificed and why the trade-off is worthwhile.
- **don't duplicate** variations already in the workspace.
- **set credence and robustness honestly, with reasoning.** the
  robust version should have higher credence than the original. in
  `robustness_reasoning`, spell out *why* the variation is sturdier
  than the original.
