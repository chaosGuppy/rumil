## the task

you're creating a **view** for a question. a view is a curated,
structured summary of the workspace's current best understanding —
the thing a new researcher should read before starting work on the
question. it's the workspace's collective intelligence on the
question, distilled into its most useful form, orienting future
instances toward productive work and away from redundant
investigation.

a view is not a wall of assertions; it's a small set of **atomic
items** organised into sections, each carrying a robustness score
(how solid the underlying position is) and an importance score (how
core it is to the view's overall picture).

view items are *not themselves claims*. they're curated observations
about the research picture. if one of your items is a sharp,
falsifiable assertion that deserves a credence score in its own
right, create a separate claim and have the view item cite it.

## a few moves

before producing items, name the cached take. what would a sharp
person in this space write as the obvious view here? write it down.
is that what you'd actually defend on the merits, or are you about
to retrieve a plausible-feeling version? the considerations and
judgements in your context are the substance — the view should
reflect what they actually say, not what a generic view on this
topic would say.

then attack the draft view: where are you balancing for balance's
sake instead of describing what the evidence actually shows? where
are you including filler items that wouldn't change a downstream
reader's actions? cut them. the view's value is in curation, not
completeness.

if the question is subtly malformed — terms that don't carve well,
multiple readings hiding inside one summary — surface that in
`broader_context` or `key_uncertainties` rather than papering over
it.

## sections

items are organised into these sections:

- **broader_context** — facts and framing that situate the question
  ("this question arises in the context of...").
- **confident_views** — positions the workspace is relatively
  confident about (typically robustness 3+ on the view item, citing
  high-credence claims).
- **live_hypotheses** — active hypotheses being considered, not yet
  well-evidenced enough for confident_views.
- **key_evidence** — important evidence that bears on the question,
  regardless of which hypothesis it supports.
- **assessments** — integrative assessments that weigh multiple
  considerations ("on balance, X seems more likely than Y because...").
- **key_uncertainties** — important things we don't know that could
  shift the picture.
- **other** — items that don't fit elsewhere.

## scoring each item

each view item has two scores:

**robustness (1-5)** — how well-investigated is the position
represented by this item? 1 wild guess, 3 considered view, 5
thoroughly tested.

**importance (1-5)** — how core to the view's overall picture?
- **5** — essential. the most important things to know about this
  question. the NL summary focuses on these.
- **4** — important context that significantly aids understanding.
- **3** — useful background that helps but isn't critical.
- **2** — noted but not load-bearing.
- **1** — marginal; may be pruned in future updates.

view items don't carry credence — that's for claims. if a view
item's underlying assertion deserves a credence score, create a
claim and cite it.

## importance caps

views have strict caps on how many items can sit at each importance
level. **respect them.** the caps are provided in the task
description. if you want to add an importance-5 item and the cap is
full, either demote an existing importance-5 item or assign the new
one importance-4. don't quietly exceed the cap.

## how to build the view

1. **survey the evidence.** read through the considerations, claims,
   judgements, and other material in your context.
2. **extract atomic items.** each item is a single, self-contained
   observation — not a paragraph. think "newspaper headline + one
   sentence of supporting reasoning."
3. **assign sections.** place each item where its role in the view
   fits best.
4. **score carefully, with reasoning.** robustness should reflect
   how solid the underlying position is. importance should reflect
   how much this item contributes to orienting someone on the
   question. every item needs `robustness_reasoning` — where the
   uncertainty sits and how reducible it is.
5. **prioritise ruthlessly.** not everything belongs in the view. if
   an observation is low-robustness and not a live hypothesis worth
   tracking, leave it out.

## item content format

each item's content follows this pattern:

- lead with the observation itself — clear, specific, self-contained.
- follow with a brief **epistemic gloss** (1-2 sentences) — what the
  robustness score means in this specific case, and where relevant
  the credences of the claims this item draws on.
- cite specific page IDs that provide supporting evidence.
