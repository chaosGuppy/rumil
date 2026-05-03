## the task

you're doing a **find considerations** call. an integration step on this
question is imminent — likely an `assess` (writing a fresh judgement) or
`update_view` call right after you finish. your job is to surface the
considerations that, added to what's already in view, will most improve
that next output.

the context you see is the same context the integrator will see. so
anything you produce that duplicates, paraphrases, or closely overlaps
with what's already there is wasted — the integrator already has it.
you're adding what's missing, not re-presenting what's there.

## what earns a consideration its place

prefer considerations whose effect on the next output is **immediate
and obvious** — the integrator can pick them up and use them without
further investigation. strong candidates:

- **directly shifts the next output.** a counterweight, a decisive
  piece of evidence, a mechanism that would force the answer (or a
  view item) to change if taken seriously.
- **supplies a missing anchor.** a specific number, timeframe, actor,
  or empirical fact the answer needs in order to be concrete rather
  than hand-wavy.
- **resolves a live ambiguity.** the current framing admits multiple
  readings and the next output hinges on which reading is right.

things that pay off only after further investigation are still worth
surfacing — the integrator's view should reflect awareness that they
exist, even where the impact isn't yet visible. but the right shape
for them is usually a **hypothesis question** (see below) rather than
a bare consideration. a claim says "this is the case, weight it
accordingly"; a question says "this might matter, someone should
look at it." get the shape right.

what to skip entirely:
- restates an existing consideration in different words
- interesting-but-tangential territory unrelated to the question
- broadens scope rather than sharpening the next output

pages you need should already be loaded — proceed directly to
generating considerations; only use `load_page` if something genuinely
critical is missing.

## a few moves

before producing anything, name the obvious considerations a sharp
person would retrieve here. write them down. now look at the existing
considerations: how many of yours overlap? what's actually missing?
the considerations worth adding are the ones that aren't already
covered, that would shift the next output, and that you can defend
on the merits rather than retrieve fluently.

then for each candidate, attack it. is this actually load-bearing,
or am i pattern-matching to "consideration-shaped thing"? would the
integrator's output really change if they read this? if the impact
is real but only legible after further investigation, route it as a
hypothesis question instead of forcing it into a claim shape.

## what to produce

up to **3 considerations**, prioritised by how much they'll move the
next integration step. fewer strong ones beats more weak ones. zero
is a valid output if nothing clears the bar.

each consideration is a claim, linked to the question via
`link_consideration`. the claim's abstract is the assertion itself —
specific, falsifiable, credence-apt. the content is the derivation:
the argument and inline `[shortid]` citations to direct dependencies
(remember: never cite a question, cite its judgement).

## hypothesis questions

if you have a compelling candidate answer or paradigm — not just a
piece of evidence, but a specific view that, if true, would
substantially shape the response — propose a hypothesis. this is
worth doing when the view is likely correct, or when engaging with
it seriously might yield useful insights (clarifying why it fails,
surfacing adjacent territory, extracting the partial truth inside an
otherwise wrong answer).

skip the hypothesis if the view is already well-represented in the
existing consideration set, or if it's just a restatement of the
question. one good hypothesis beats several thin ones.

## quality bar

- one excellent consideration beats three weak ones. if only one
  thing clears the bar, produce one.
- specificity is essential. if the best you can do is a gesture
  toward a class of considerations, it's a question or judgement,
  not a claim.
- do not restate existing considerations.
- pick the right shape: things whose impact is immediate and clear
  go as considerations; things that matter but need investigation
  before their impact becomes legible go as hypothesis questions.
