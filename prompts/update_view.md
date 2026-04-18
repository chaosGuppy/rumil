# Update View

You are incrementally updating an existing **View page** for a question. Unlike creating a View from scratch, you are reviewing and refining items that already exist, scoring newly proposed items, and optionally proposing new items where the View has gaps.

The View consists of **atomic items** organized into **sections**, each with epistemic scores. Your job across multiple phases is to bring the View up to date with the current evidence.

<!-- PHASE:context — DO NOT RENAME THIS MARKER -->
## Shared Context

A View is a curated, structured summary of the workspace's current best understanding on a research question. Each View item is a short, self-contained observation with:

* **Robustness (1-5)**: How well-investigated (1=wild guess, 3=considered view, 5=thoroughly tested)
* **Importance (1-5)**: How core to the View (5=essential, 1=marginal)

View items do **not** carry a credence score — credence applies only to claim pages. If a View item's underlying assertion is sharp and falsifiable enough to deserve a credence score, a separate claim page should exist (or be created) and the View item should cite it.

**Sections:** broader\_context, confident\_views, live\_hypotheses, key\_evidence, assessments, key\_uncertainties, other.

You will be asked to review View items in batches across several phases. Apply your judgement carefully — small, well-justified changes are better than sweeping revisions.

The context may include a **Child Investigation Results** section showing the latest findings from recursive sub-question investigations. Items marked **[NEW]** were produced since the View was last updated. These results should inform your review — new findings may confirm, contradict, or refine existing View items, or may reveal gaps needing new items.

<!-- PHASE:score_unscored — DO NOT RENAME THIS MARKER -->
## Phase: Score Unscored Proposals

These items were proposed by other calls but have not yet been scored. For each item, assign:

* **importance** (1-5): How core is this item to the View?
* **section**: Which section best fits this item's role?
* **robustness** (optional): Override if the current score looks wrong. If you set this, you must also provide **robustness_reasoning** — per the preamble rubric, explain where the uncertainty sits and how reducible it is.

Do not enforce importance caps at this stage — just score each item on its merits.

<!-- PHASE:triage — DO NOT RENAME THIS MARKER -->
## Phase: Triage

You are doing a shallow review of existing View items. For each item, decide:

* **ok**: The item looks fine — scores are reasonable, content is accurate, section is appropriate. No changes needed.
* **review**: Something about this item warrants a closer look — the scores may be off, the content may be outdated or inaccurate, it may be in the wrong section, or it may need to be replaced with a better formulation.

Err on the side of flagging items for review. It is better to review an item that turns out to be fine than to miss one that needs updating. But do not flag everything — focus on items where you see a specific reason for concern.

<!-- PHASE:deep_review — DO NOT RENAME THIS MARKER -->
## Phase: Deep Review

For each item in this batch, choose one action:

* **keep**: The item is fine as-is after closer inspection. No changes.
* **adjust**: The item's scores or section assignment need updating. Provide the new values and brief reasoning. If you change the robustness score, you must also provide `new_robustness_reasoning` explaining where the uncertainty stems from and how reducible it is.
* **supersede**: The item should be replaced with a new version. Provide a new headline, content, robustness, robustness_reasoning, importance, and section. The old item will be superseded and the new one linked to the View.

When superseding, write the replacement item as you would a fresh View item: a clear headline, content with an epistemic gloss explaining the robustness score, and careful scoring. Provide `robustness_reasoning` per the preamble rubric — where the uncertainty sits and how reducible it is.

You may also **propose entirely new items** if you notice gaps — evidence or conclusions that the View should capture but currently doesn't. New items should include full scores (robustness, robustness_reasoning, importance, section) and follow the same format as existing items.

<!-- PHASE:enforce_caps — DO NOT RENAME THIS MARKER -->
## Phase: Importance Cap Enforcement

The View has importance caps that limit how many items can exist at each importance level. The items shown below are at a level that currently exceeds its cap.

Choose which items to **demote** (lower their importance score). For each item you demote, provide the new importance level and brief reasoning for why this item is less essential than the others at this level.

Keep the items that are most central to understanding the question. Demote items that are useful context but not as critical.

<!-- PHASE:prune — DO NOT RENAME THIS MARKER -->
## Phase: Prune Low-Value Items

These are importance-1 and importance-2 items. Decide for each:

* **keep**: The item still belongs in the View, even at low importance.
* **remove**: The item adds little value and should be dropped from the View. This could be because it is redundant with higher-importance items, no longer relevant, or too marginal to justify inclusion.

Be willing to remove items that are not pulling their weight. A tighter View is more useful than a comprehensive one. But do not remove items that provide genuinely useful context or that track uncertainties worth monitoring.
