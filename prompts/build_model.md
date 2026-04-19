# Build Model — theoretical

You are building a **toy theoretical model** of the phenomenon the scope question is about. Your output is a single MODEL page (filled in via `write_model_body`) plus one page per quantitative prediction the model generates.

The goal is not to produce a polished, peer-reviewed model. It is to **make the reasoning structure explicit** so that downstream instances can attack, refine, or supersede it. A good toy model is compact, honest about what it's assuming, and generates predictions that would be clearly wrong under some possible worlds.

## Why separate pages for the model and its predictions?

The MODEL page is a single unit: variables, relations, parameters, and assumptions are tightly coupled and usually get revised together. Superseding the MODEL page wholesale is the natural editing move.

Predictions are different. Each prediction is its own falsifiable claim and should be attackable one-at-a-time. A scout that looks for how a specific prediction could be false (`scout_c_how_false`) needs the prediction as its own page with its own credence score.

## MODEL page body — structure

When you call `write_model_body`, use this structure. Every section is required. Use Markdown.

```
## Variables

| name | units | plausible range | credence | notes |
| --- | --- | --- | --- | --- |
| N | count of target-market organizations | 5,000–50,000 | 7/9 on the range | US mid-market SaaS buyers |
| p | fraction adopting per quarter via word of mouth | 0.01–0.15 per quarter | 5/9 | highly context-dependent |
| ...

## Relations

State each relation as an equation (or tight qualitative mechanism if math isn't apt), followed by a per-relation credence and one-sentence mechanism note.

- **Bass-style adoption:** dA/dt = (p + q·A/N) · (N − A). Credence 6/9. Captures innovators (p) + imitators (q·A/N) on a saturating pool.
- **Saturation:** A(t) → N as t → ∞. Credence 8/9. Tautological given the differential above.
- ...

## Parameters

For every parameter that appears in relations: the value (or range), the unit, the source or justification, and a one-sentence sensitivity note.

- p = 0.03/quarter, justified by industry benchmark for enterprise SaaS innovators; doubling p roughly halves time-to-50%-penetration.
- ...

## Predictions

List each prediction as a one-sentence falsifiable claim, tagged with the variable(s) it concerns. Don't write the CLAIM pages here — list the *content* of each prediction so you can emit them as separate `create_claim` (or `propose_view_item`) moves after this.

- **P1.** Time-to-50%-penetration for an average-p product in this market is 8–14 quarters.
- **P2.** Doubling marketing spend during the inflection (quarters 4–6) shortens time-to-50% by ~1.5 quarters, not 4.
- ...

## Assumptions

One line each. Each assumption is a candidate sub-claim for future investigation.

- A1. The target-market size N is approximately fixed over the relevant horizon.
- A2. Word-of-mouth rate q is constant, not time-varying.
- ...

## Sensitivities

Which 2–3 parameters or assumptions is the headline prediction most sensitive to? What would the prediction look like if each of those flipped?

- P1 is most sensitive to q; halving q doubles time-to-50%.
- P2 is most sensitive to A2; if word-of-mouth saturates, the effect of marketing during inflection grows.
```

## Concrete example: adoption-curve model (shortened)

If the scope question is "What factors drive the adoption curve of a new software product?", a minimal body might look like:

> ## Variables
> - `N` (count, 5k–50k): addressable market size. Credence 6/9 on that range for US mid-market SaaS.
> - `p` (fraction/quarter, 0.01–0.05): innovator adoption rate, independent of how many have adopted. Credence 5/9.
> - `q` (fraction/quarter, 0.05–0.30): imitator adoption rate per already-adopted contact. Credence 4/9.
>
> ## Relations
> - Bass diffusion (credence 6/9): dA/dt = (p + q·A/N)(N − A).
> - Saturation (credence 8/9): dA/dt → 0 as A → N.
>
> ## Parameters
> - p = 0.03 (benchmark), q = 0.15 (benchmark). Sensitivity: time-to-50% is ≈ 1/q to within 20%.
>
> ## Predictions
> - P1. Time-to-50%-penetration for a product with industry-typical (p, q) is 6–10 quarters.
> - P2. A product with q below 0.05 will not reach 30% penetration within 5 years.
>
> ## Assumptions
> - A1. N is fixed on the 5-year horizon.
> - A2. p and q are constants, not time-varying.
> - A3. Churn is negligible on the adoption-curve timescale.
>
> ## Sensitivities
> - P1 is most sensitive to q. If q = 0.05 rather than 0.15, P1 pushes out to 18–30 quarters.
> - P2 is most sensitive to A3 — if churn is material, the "never reaches 30%" prediction is weaker.

## Emitting predictions

After `write_model_body` returns, for each prediction in the body:

- If the scope question has a View, call `propose_view_item` to add the prediction as an unscored View item. The next assess/update_view call will score and slot it.
- Otherwise, call `create_claim` with the prediction as a standalone CLAIM (pick a credence based on how confident the model makes you in the prediction, and a robustness that reflects how well-supported the derivation is). Use the `links` field to link the claim to the scope question as a consideration — or call `link_consideration` separately.

Every prediction you emit should cite the MODEL page inline with `[shortid]` so the workspace's dependency graph picks up the fact that the prediction rests on this model.

## Robustness score for the MODEL page itself

Typical toy models should be robustness 2–3:
- **2 = informed impression.** The relations are defensible but have not been empirically tested against this specific domain.
- **3 = considered view.** The relations are drawn from an established theory (Bass diffusion, S-curves, supply-and-demand) and the parameter ranges are calibrated against published benchmarks.

A 4–5 model would need direct empirical grounding (a fitted Bass curve on this specific product category) or a formal derivation. Don't inflate robustness for a first-draft theoretical model — the point is to name the mechanisms clearly, not to claim they're correct.

## What *not* to do

- Do not dump a 5-page essay into the body. The structure above is load-bearing; use it.
- Do not mix predictions into the MODEL page body as if they were free-text — they go in the Predictions section as one-liners, then get emitted as separate pages.
- Do not cite the MODEL page from itself.
- Do not pretend the model is more robust than it is. Under-confidence about relations is a feature; it's what makes downstream scout attacks tractable.
