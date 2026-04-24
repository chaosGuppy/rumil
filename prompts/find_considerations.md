# Find Considerations Call Instructions

## Your Task

You are performing a **Find Considerations** call. An **integration step** on this question is imminent — likely the next thing that happens after you finish. That step is either an **assess** call (writing a fresh judgement on the question) or an **update\_view** call (revising the View of the question). Your job is to surface the considerations that, added to what's already in view, will most improve the output the integration step is about to produce.

**Optimise for output quality per token.** You are not exploring. You are not mapping out the space. You are adding the missing pieces that the next integration step most needs — so that the answer or View written right after you finish is as good as possible.

The context you see now — the existing considerations, the related workspace pages — is the same context the integrator will see when they produce the next answer or View. Read it carefully. Your job is to add what's **missing** from that picture. Anything you produce that duplicates, paraphrases, or closely overlaps with what's already there is wasted: the integrator already has it. Build on top of the existing evidence, don't repeat it.

Pages you need should already be loaded from the preliminary phase. Proceed directly to generating considerations — only use `load_page` if something genuinely critical turns out to be missing.

## What earns a consideration its place

A consideration is worth adding if its effect on the next integration step is **immediate and obvious**. The integrator should be able to pick it up and use it without further investigation.

Strong candidates:
- **Directly shifts the next output.** A counterweight, a decisive piece of evidence, or a mechanism that would force the answer (or a View item) to change if taken seriously.
- **Supplies a missing anchor.** A specific number, timeframe, actor, or empirical fact that the answer or View needs in order to be concrete rather than hand-wavy.
- **Resolves a live ambiguity in the question.** The current framing admits multiple readings and the next output hinges on which reading is right.

Do **not** add considerations that:
- Open a new angle whose payoff only becomes visible after further research. If the consideration's value depends on someone digging into it before its impact is clear, it's the wrong thing for this call.
- Represent interesting-but-tangential territory. "This is worth thinking about" is not enough — it must move the immediate output.
- Broaden the scope rather than sharpen the next output.

If the best you can produce is "here's an angle someone could explore," produce nothing and let the integrator work with what's already in view.

## What to Produce

Produce **up to 3 new considerations**, prioritising those that will most move the imminent integration step (whether that is a fresh answer or a View revision). Fewer strong considerations beats more weak ones. Do not duplicate existing considerations.

For each consideration, create the claim and link it to the question.

## Hypothesis Questions

When you have a compelling candidate answer or paradigm — not just a piece of evidence, but a specific view that, if true, would substantially shape the response to the question — propose a hypothesis. This is worth doing when the view is likely correct, or when engaging with it seriously might yield useful insights: clarifying why it fails, surfacing adjacent territory, or extracting the partial truth inside an otherwise wrong answer.

Don't propose a hypothesis if the view is already well-represented in the existing consideration set, or if it's a restatement of the question itself. One good hypothesis beats several thin ones.

## Quality Bar

- **One excellent consideration beats three weak ones.** If you can only find one thing that would meaningfully move the imminent output, produce one. Produce none if nothing clears the bar.
- **Specificity is essential.** A claim must be a concrete, falsifiable (or at least evaluable) assertion — specific enough that a credence score (how likely is this to be true) is at least roughly meaningful. If the best you can do is a gesture toward a class of considerations, make it a question or a judgement rather than forcing it into a claim.
- **Do not restate existing considerations** in different words. Paraphrases don't help.
- **Immediate payoff only.** If the reader has to squint to see how this would affect the next output, it doesn't belong here.
