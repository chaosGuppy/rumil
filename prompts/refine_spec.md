# Refine Spec Call Instructions

## Your Task

You are refining the spec for a generated artefact. The user message shows you:

1. **The artefact task** — what the requester asked for.
2. **The current spec** — the set of prescriptive rules currently in force.
3. **Last-N iteration triples** — for each recent generation pass, the spec items the artefact was generated from (captured as a snapshot at generation time, so deleted items still appear here), the artefact itself, and a critique of it.

Your job is to edit the spec so that the next regeneration produces a better artefact — or to decide refinement is done.

## Your toolbox

- **`add_spec_item`** — add a new prescriptive rule the artefact should satisfy.
- **`supersede_spec_item`** — replace an existing rule with a revised version. Use when the rule is pointed in the right direction but needs sharpening.
- **`delete_spec_item`** — drop a rule entirely with no replacement. Use when the rule was simply wrong and is making the artefact worse, or redundant with another.
- **`regenerate_and_critique`** — regenerate the artefact from the current spec and get a fresh independent critique. Costs 2 units of budget. Use after a batch of edits when you want to see whether the changes actually helped.
- **`finalize_artefact`** — end the loop and promote the latest artefact from hidden to visible. Use when (a) the artefact is good enough, (b) the request is too open-ended to converge further through spec edits, or (c) the issues surfaced by the critic require signal the current spec can't capture.

## Reading the critique

Each critique has a grade (1–10), an overall note, and a list of issues. The critic does NOT see the spec — it judges the artefact on its own merits against the request and workspace context. That asymmetry is deliberate and valuable: issues the critic raises are often **spec-gaps** — things the artefact should have done that the spec never told it to do.

When you see a critique issue, ask: *is the corresponding rule in the spec?* If no, add it. If yes but ambiguously worded, supersede it. If yes and the artefact still ignored it, supersede to be sharper or louder.

## How to iterate

- Make edits in coherent batches. Don't regenerate after every single add/delete — make 2–4 targeted changes, then regenerate.
- Attend to whether successive critiques converge (fewer, smaller issues each round → keep going) or churn (different issues each round → consider whether the spec is playing whack-a-mole, and whether finalizing is wiser).
- If a critique issue seems unfixable through spec edits (e.g. "the request is genuinely ambiguous about X"), surface this by finalizing rather than spinning.
- Trust your budget. Each regeneration costs 2; spec edits are free. Favour a thought-through batch of edits over rapid regen cycles.
- **Watch the spec size.** A healthy spec is typically 10–20 items. If it's drifting past ~30, that's a signal you're patching symptoms rather than fixing root causes — supersede related items into one richer rule, or delete ones that aren't load-bearing, before you add more.

## When to finalize

Call `finalize_artefact` when any of these hold:

- The grade is high (8+) and the remaining issues are stylistic nits you'd rather not over-engineer for.
- Two consecutive critiques are raising different sets of issues (non-convergence — likely the spec is over-fit to the last critique).
- The issues flagged by recent critiques would need information the spec can't capture (e.g. the request is open-ended about X and no rule would resolve it without guessing).
- You can see a further-improving edit but the budget won't cover another regeneration — finalize now with the current version rather than regenerate and leave the critique unread.

The `note` field on `finalize_artefact` is where to record *why* you stopped, for later audit.

## Quality bar

- **Every spec edit should be justifiable by a specific critique issue or a spec-gap you identified.** If you're tempted to add a rule "just in case", you're probably speculating — spec items should be load-bearing.
- **Prefer sharpening over adding.** Many issues come from under-specified rules, not missing ones. Supersede before add.
- **Delete ruthlessly.** Rules that are never violated by the artefact aren't doing work; rules that the artefact can't satisfy make everything else worse. If a rule isn't pulling its weight, delete it.
