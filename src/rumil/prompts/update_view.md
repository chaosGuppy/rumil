# update view

you're incrementally updating an existing **view** for a question.
unlike creating a view from scratch, you're reviewing and refining
items that already exist, scoring newly proposed items, and
optionally proposing new items where the view has gaps.

the view consists of **atomic items** organised into **sections**,
each with epistemic scores. your job across multiple phases is to
bring the view up to date with the current evidence.

<!-- PHASE:context — DO NOT RENAME THIS MARKER -->

## shared context

a view is a curated, structured summary of the workspace's current
best understanding on a research question. each view item is a
short, self-contained observation with:

- **robustness (1-5):** how well-investigated. 1 wild guess, 3
  considered view, 5 thoroughly tested.
- **importance (1-5):** how core to the view. 5 essential, 1
  marginal.

view items do **not** carry credence — credence applies only to
claim pages. if a view item's underlying assertion is sharp and
falsifiable enough to deserve a credence score, a separate claim
page should exist (or be created) and the view item should cite it.

**sections:** broader_context, confident_views, live_hypotheses,
key_evidence, assessments, key_uncertainties, other.

you'll be asked to review view items in batches across several
phases. apply your judgement carefully — small, well-justified
changes beat sweeping revisions.

the context may include a **child investigation results** section
showing the latest findings from recursive sub-question
investigations. items marked **[NEW]** were produced since the view
was last updated. these results should inform your review — new
findings may confirm, contradict, or refine existing view items, or
may reveal gaps needing new items.

the context may also include a **recent findings from claim
investigations** section. claim investigations drill into specific
considerations of this question and often produce counter-evidence
or reframings that live at the claim level and haven't yet been
reflected in the view. treat these findings the same way as
child-investigation results: they should directly pressure existing
view items or motivate new ones. if a finding here materially
contradicts a view item, supersede or adjust that item rather than
leaving both standing.

before any of these phases, take a moment to name the cached take.
what's the obvious "this view looks fine / this view needs major
revision" reaction a sharp person would have? write it down. then
check it against specifics — sometimes the cached read is right;
sometimes the new findings reveal pressure the cached read missed.

<!-- PHASE:score_unscored — DO NOT RENAME THIS MARKER -->

## phase: score unscored proposals

these items were proposed by other calls but haven't yet been
scored. for each item, assign:

- **importance** (1-5): how core is this item to the view?
- **section:** which section best fits this item's role?
- **robustness** (optional): override if the current score looks
  wrong. if you set this, you must also provide
  **robustness_reasoning** — explain where the uncertainty sits and
  how reducible it is.

don't enforce importance caps at this stage — score each item on
its merits.

<!-- PHASE:triage — DO NOT RENAME THIS MARKER -->

## phase: triage

shallow review of existing view items. for each item, decide:

- **ok:** the item looks fine — scores are reasonable, content is
  accurate, section is appropriate. no changes needed.
- **review:** something about this item warrants a closer look —
  the scores may be off, the content may be outdated or inaccurate,
  it may be in the wrong section, or it may need to be replaced
  with a better formulation.

err on the side of flagging items for review. it's better to review
an item that turns out to be fine than to miss one that needs
updating. but don't flag everything — focus on items where you see
a specific reason for concern.

<!-- PHASE:deep_review — DO NOT RENAME THIS MARKER -->

## phase: deep review

for each item in this batch, choose one action:

- **keep:** the item is fine as-is after closer inspection. no
  changes.
- **adjust:** the item's scores or section assignment need updating.
  provide the new values and brief reasoning. if you change the
  robustness score, you must also provide `new_robustness_reasoning`
  explaining where the uncertainty stems from and how reducible it
  is.
- **supersede:** the item should be replaced with a new version.
  provide a new headline, content, robustness, robustness_reasoning,
  importance, and section. the old item will be superseded and the
  new one linked to the view.

when superseding, write the replacement item as you would a fresh
view item: a clear headline, content with an epistemic gloss
explaining the robustness score, and careful scoring. provide
`robustness_reasoning` per the preamble rubric — where the
uncertainty sits and how reducible it is.

each item is rendered with its **cited evidence** (pages the item
already depends on) and, when applicable, a **related considerations
on the parent question (not cited by this item)** block. the latter
lists considerations of the scope question that the item doesn't
already cite — these are the most likely source of overlooked
contradictions or complications. before choosing an action, scan
this list and ask whether any of these considerations should
change, complicate, or replace the item under review.

this phase only acts on the items shown. **don't propose net-new
items here** — a dedicated *propose new items* phase follows that
handles gap-filling separately.

<!-- PHASE:propose_new — DO NOT RENAME THIS MARKER -->

## phase: propose new items

you've now reviewed the existing items. in this phase you decide
whether anything important is **missing** from the view —
observations the view should capture but currently doesn't.

the most likely sources of gaps:

- findings in the **child investigation results** section
  (especially items marked **[NEW]**) whose synthesis isn't
  reflected in any current view item — for example, a child view
  whose top-line answer or load-bearing crux contradicts or simply
  isn't present in any parent-view item.
- findings in the **recent findings from claim investigations**
  section that change the picture (new mechanisms, sign flips,
  magnitude updates, missing channels) and aren't yet captured.
- considerations on the parent question that point at structurally
  important content (a missing channel under the question's
  framing, a key empirical anchor, a load-bearing uncertainty) which
  no existing item addresses.

each proposal must be a **net-new** item, not a restatement of an
existing one. before proposing an item, check the current view
state shown below: if a current item already covers the
observation, don't propose a duplicate — flag it for `review` in a
future call instead, or trust that earlier triage already handled
it.

apply the same standards as when creating a view from scratch:

- **atomic and self-contained.** one observation per item —
  newspaper headline plus one sentence of supporting reasoning.
- **section.** place the item where it does the most orienting
  work.
- **importance.** reserve I=5 for items the executive summary
  should lead with; I=4 for important context; I=3 for useful
  background; I=2 for noted-but-not-load-bearing. the next phase
  enforces caps, so honest scoring matters.
- **robustness + reasoning.** score how well-investigated the
  underlying position is and explain where the residual uncertainty
  sits.
- **cite sources.** reference page IDs (e.g. `[abc12345]`) for any
  specific claims, child views, or evidence the item rests on.
  items without grounding don't earn a place.

be selective. curation beats completeness — an empty proposal list
is the right answer when the view already captures the important
picture. but when there's a genuine gap, propose the item: views
that fail to absorb new findings stop being useful summaries.

<!-- PHASE:enforce_caps — DO NOT RENAME THIS MARKER -->

## phase: importance cap enforcement

the view has importance caps that limit how many items can exist at
each importance level. the items shown below are at a level that
currently exceeds its cap.

choose which items to **demote** (lower their importance score).
for each item you demote, provide the new importance level and
brief reasoning for why this item is less essential than the others
at this level.

keep the items that are most central to understanding the question.
demote items that are useful context but not as critical.

<!-- PHASE:prune — DO NOT RENAME THIS MARKER -->

## phase: prune low-value items

these are importance-1 and importance-2 items. decide for each:

- **keep:** the item still belongs in the view, even at low
  importance.
- **remove:** the item adds little value and should be dropped from
  the view. could be because it's redundant with higher-importance
  items, no longer relevant, or too marginal to justify inclusion.

be willing to remove items that aren't pulling their weight. a
tighter view is more useful than a comprehensive one. but don't
remove items that provide genuinely useful context or that track
uncertainties worth monitoring.
