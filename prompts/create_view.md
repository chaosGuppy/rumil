# Create View

You are creating a **View page** for a question. A View is a curated, structured summary of the current best understanding — the thing a new researcher should read before starting work on this question.

## What a View Is

A View consists of **atomic items** organized into **sections**. Each item is a short, self-contained claim or observation with epistemic scores. Together, the items in a View represent the workspace's current view on the question.

Think of the View as the workspace's collective intelligence on this question, distilled into its most useful form. It should orient future instances toward productive work and away from redundant investigation.

## Sections

Items are organized into these sections:

* **broader\_context**: Facts and framing that situate the question (e.g., "This question arises in the context of...")
* **confident\_views**: Claims the workspace is relatively confident about (typically C7+ and R3+)
* **live\_hypotheses**: Active hypotheses being considered — not yet well-evidenced enough for confident\_views
* **key\_evidence**: Important evidence that bears on the question, regardless of which hypothesis it supports
* **assessments**: Integrative assessments that weigh multiple considerations (e.g., "On balance, X seems more likely than Y because...")
* **key\_uncertainties**: Important things we don't know that could shift the picture
* **other**: Items that don't fit neatly elsewhere

## Scoring Each Item

Each item has three scores:

**Credence (1-9)**: How likely is this to be true? See the preamble for the full scale.

**Robustness (1-5)**: How well-investigated is this? 1=wild guess, 3=considered view, 5=thoroughly tested.

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
2. **Extract atomic items.** Each item should be a single, self-contained claim or observation — not a paragraph. Think "newspaper headline + one sentence of supporting reasoning." The content field should include an **epistemic gloss**: 1-2 sentences explaining what the credence and robustness scores mean in this specific case.
3. **Assign sections.** Place each item in the section that best fits its role in the View.
4. **Score carefully.** Credence and robustness should reflect the actual evidence, not aspirations. Importance should reflect how much this item contributes to orienting someone on the question.
5. **Prioritize ruthlessly.** Not everything belongs in the View. If a claim is low-credence, low-robustness, and not a live hypothesis worth tracking, leave it out. The View's value comes from curation, not completeness.

## Item Content Format

Each item's content should follow this pattern:

* Lead with the claim itself (clear, specific, self-contained)
* Follow with a brief epistemic gloss in parentheses: why you assigned these C/R scores
* Optionally reference specific page IDs that provide supporting evidence
