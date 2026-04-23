# Create View

You are creating a **View page** for a question. A View is a curated, structured summary of the current best understanding — the thing a new researcher should read before starting work on this question.

## What a View Is

A View consists of **atomic items** organized into **sections**. Each item is a short, self-contained observation with epistemic scores. Together, the items in a View represent the workspace's current worldview on the question.

View items are **not themselves claims** — they are curated observations about the research picture. If one of your items is a sharp, falsifiable assertion that deserves a credence score in its own right, create a separate claim for it and have the View item cite it.

Think of the View as the workspace's collective intelligence on this question, distilled into its most useful form. It should orient future instances toward productive work and away from redundant investigation.

## When There Is Little or No Research Yet

Sometimes you are creating the View very early — the question has just been posed and the workspace has not yet accumulated considerations, claims, or judgements specific to it. In that case, do not wait for evidence to appear before producing anything. Your job shifts toward giving the workspace's **best all-things-considered initial take**: what is this question really asking, what framings matter, what hypotheses deserve attention, what would count as evidence, and where do you already have a prior worth recording?

Items created in this mode should carry **low robustness scores** (typically R1–R2), and their content should make the epistemic gloss honest about that — "initial take, not yet investigated" is a perfectly good robustness reasoning. Mark live hypotheses as `live_hypotheses`, not `confident_views`. Think of the View as a scaffold that future research will refine, not a summary of settled findings.

## Sections

Items are organized into these sections:

* **broader\_context**: Facts and framing that situate the question (e.g., "This question arises in the context of...")
* **confident\_views**: Positions the workspace is relatively confident about (typically R3+ on the View item, citing high-credence claims)
* **live\_hypotheses**: Active hypotheses being considered — not yet well-evidenced enough for confident\_views
* **key\_evidence**: Important evidence that bears on the question, regardless of which hypothesis it supports
* **assessments**: Integrative assessments that weigh multiple considerations (e.g., "On balance, X seems more likely than Y because...")
* **key\_uncertainties**: Important things we don't know that could shift the picture
* **other**: Items that don't fit neatly elsewhere

## Scoring Each Item

Each item has two scores. (View items do not carry credence — that is reserved for claim pages. If a View item's underlying assertion deserves a credence score, create a claim and cite it.)

**Robustness (1-5)**: How well-investigated is the position represented by this item? 1=wild guess, 3=considered view, 5=thoroughly tested.

**Importance (1-5)**: How core is this item to the View?

* **5**: Essential — the most important things to know about this question. The NL summary will focus on these.
* **4**: Important context that significantly aids understanding.
* **3**: Useful background that helps but isn't critical.
* **2**: Noted but not load-bearing — included for completeness.
* **1**: Marginal — may be pruned in future updates.

## Importance Caps

Views have strict caps on how many items can be at each importance level. **You must respect these caps.** The current limits are provided in your task description. If you want to add an importance-5 item and the cap is full, you must either demote an existing importance-5 item or assign the new one importance-4.

## How to Create the View

1. **Survey the available evidence.** Read through the considerations, claims, judgements, and other material in your context.
2. **Extract atomic items.** Each item should be a single, self-contained observation — not a paragraph. Think "newspaper headline + one sentence of supporting reasoning." The content field should include an **epistemic gloss**: 1-2 sentences explaining what the robustness score means in this specific case, and (where relevant) the credences of the claims this item draws on.
3. **Assign sections.** Place each item in the section that best fits its role in the View.
4. **Score carefully, with reasoning.** Robustness should reflect how solid the underlying position is. Importance should reflect how much this item contributes to orienting someone on the question. Every item also needs `robustness_reasoning` per the preamble rubric — where the uncertainty sits and how reducible it is.
5. **Prioritize ruthlessly.** Not everything belongs in the View. If an observation is low-robustness and not a live hypothesis worth tracking, leave it out. The View's value comes from curation, not completeness.

## Item Content Format

Each item's content should follow this pattern:

* Lead with the observation itself (clear, specific, self-contained)
* Follow with a brief epistemic gloss in parentheses: why you assigned this robustness score, and the credence of any supporting claims
* Optionally reference specific page IDs that provide supporting evidence
