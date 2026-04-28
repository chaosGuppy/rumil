# Update View

You are incrementally updating an existing **View page** for a question. Unlike creating a View from scratch, you are reviewing and refining items that already exist, scoring newly proposed items, and optionally proposing new items where the View has gaps.

The View consists of **atomic items** organized into **sections**, each with epistemic scores. Your job across multiple phases is to bring the View up to date with the current evidence.

<!-- PHASE:context — DO NOT RENAME THIS MARKER -->

## Shared Context

A View is a curated, structured summary of the workspace's current best understanding on a research question. Each View item is a short, self-contained observation with:

- **Robustness (1-5)**: How well-investigated (1=wild guess, 3=considered view, 5=thoroughly tested)
- **Importance (1-5)**: How core to the View (5=essential, 1=marginal)

View items do **not** carry a credence score — credence applies only to claim pages. If a View item's underlying assertion is sharp and falsifiable enough to deserve a credence score, a separate claim page should exist (or be created) and the View item should cite it.

**Sections:** broader_context, confident_views, live_hypotheses, key_evidence, assessments, key_uncertainties, other.

You will be asked to review View items in batches across several phases. Apply your judgement carefully — small, well-justified changes are better than sweeping revisions.

The context may include a **Child Investigation Results** section showing the latest findings from recursive sub-question investigations. Items marked **[NEW]** were produced since the View was last updated. These results should inform your review — new findings may confirm, contradict, or refine existing View items, or may reveal gaps needing new items.

The context may also include a **Recent findings from claim investigations** section. Claim investigations drill into specific considerations of this question and often produce counter-evidence or reframings that live at the claim level and have not yet been reflected in the View. Treat these findings the same way as child-investigation results: they should directly pressure existing View items or motivate new ones. If a finding here materially contradicts a View item, supersede or adjust that item rather than leaving both standing.

<!-- PHASE:score_unscored — DO NOT RENAME THIS MARKER -->

## Phase: Score Unscored Proposals

These items were proposed by other calls but have not yet been scored. For each item, assign:

- **importance** (1-5): How core is this item to the View?
- **section**: Which section best fits this item's role?
- **robustness** (optional): Override if the current score looks wrong. If you set this, you must also provide **robustness_reasoning** — per the preamble rubric, explain where the uncertainty sits and how reducible it is.

Do not enforce importance caps at this stage — just score each item on its merits.

<!-- PHASE:triage — DO NOT RENAME THIS MARKER -->

## Phase: Triage

You are doing a shallow review of existing View items. For each item, decide:

- **ok**: The item looks fine — scores are reasonable, content is accurate, section is appropriate. No changes needed.
- **review**: Something about this item warrants a closer look — the scores may be off, the content may be outdated or inaccurate, it may be in the wrong section, or it may need to be replaced with a better formulation.

Err on the side of flagging items for review. It is better to review an item that turns out to be fine than to miss one that needs updating. But do not flag everything — focus on items where you see a specific reason for concern.

<!-- PHASE:deep_review — DO NOT RENAME THIS MARKER -->

## Phase: Deep Review

For each item in this batch, choose one action:

- **keep**: The item is fine as-is after closer inspection. No changes.
- **adjust**: The item's scores or section assignment need updating. Provide the new values and brief reasoning. If you change the robustness score, you must also provide `new_robustness_reasoning` explaining where the uncertainty stems from and how reducible it is.
- **supersede**: The item should be replaced with a new version. Provide a new headline, content, robustness, robustness_reasoning, importance, and section. The old item will be superseded and the new one linked to the View.

When superseding, write the replacement item as you would a fresh View item: a clear headline, content with an epistemic gloss explaining the robustness score, and careful scoring. Provide `robustness_reasoning` per the preamble rubric — where the uncertainty sits and how reducible it is.

Each item is rendered with its **Cited evidence** (pages the item already depends on) and, when applicable, a **Related considerations on the parent question (not cited by this item)** block. The latter lists considerations of the scope question that the item does not already cite — these are the most likely source of overlooked contradictions or complications. Before choosing an action, scan this list and ask whether any of these considerations should change, complicate, or replace the item under review.

You may also **propose entirely new items** if you notice gaps — evidence or conclusions that the View should capture but currently doesn't. New items should include full scores (robustness, robustness_reasoning, importance, section) and follow the same format as existing items.

<!-- PHASE:propose_new — DO NOT RENAME THIS MARKER -->

## Phase: Propose New Items

You have now reviewed the existing items. In this phase you decide whether anything important is **missing** from the View — observations the View should capture but currently doesn't.

The most likely sources of gaps are:

- Findings in the **Child Investigation Results** section (especially items marked **[NEW]**) whose synthesis is not reflected in any current View item — for example, a child View whose top-line answer or load-bearing crux contradicts or simply isn't present in any parent-View item.
- Findings in the **Recent findings from claim investigations** section that change the picture (new mechanisms, sign flips, magnitude updates, missing channels) and aren't yet captured.
- Considerations on the parent question that point at structurally important content (a missing channel under the question's framing, a key empirical anchor, a load-bearing uncertainty) which no existing item addresses.

Each proposal must be a **net-new** item, not a restatement of an existing one. Before proposing an item, check the current View state shown below: if a current item already covers the observation, do not propose a duplicate — flag it for `review` in a future call instead, or trust that earlier triage already handled it.

Apply the same standards as when creating a View from scratch:

- **Atomic and self-contained.** One observation per item — newspaper headline plus one sentence of supporting reasoning.
- **Section.** Place the item where it does the most orienting work.
- **Importance.** Reserve I=5 for items the executive summary should lead with; I=4 for important context; I=3 for useful background; I=2 for noted-but-not-load-bearing. The next phase enforces caps, so honest scoring matters.
- **Robustness + reasoning.** Score how well-investigated the underlying position is and explain where the residual uncertainty sits.
- **Cite sources.** Reference page IDs (e.g. `[abc12345]`) for any specific claims, child views, or evidence the item rests on. Items without grounding don't earn a place.

Be selective. Curation beats completeness — an empty proposal list is the right answer when the View already captures the important picture. But when there's a genuine gap, propose the item: views that fail to absorb new findings stop being useful summaries.

<!-- PHASE:enforce_caps — DO NOT RENAME THIS MARKER -->

## Phase: Importance Cap Enforcement

The View has importance caps that limit how many items can exist at each importance level. The items shown below are at a level that currently exceeds its cap.

Choose which items to **demote** (lower their importance score). For each item you demote, provide the new importance level and brief reasoning for why this item is less essential than the others at this level.

Keep the items that are most central to understanding the question. Demote items that are useful context but not as critical.

<!-- PHASE:prune — DO NOT RENAME THIS MARKER -->

## Phase: Prune Low-Value Items

These are importance-1 and importance-2 items. Decide for each:

- **keep**: The item still belongs in the View, even at low importance.
- **remove**: The item adds little value and should be dropped from the View. This could be because it is redundant with higher-importance items, no longer relevant, or too marginal to justify inclusion.

Be willing to remove items that are not pulling their weight. A tighter View is more useful than a comprehensive one. But do not remove items that provide genuinely useful context or that track uncertainties worth monitoring.
