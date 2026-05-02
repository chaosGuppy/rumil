## the task

you're doing a **generate spec** call — the first step of a
generator-refiner workflow that will produce an artefact (a plan,
document, design, or other long-form object) in response to the
artefact-task question.

your job here is **not** to write the artefact. your job is to
write a **spec**: a set of prescriptive rules the artefact will be
held to. downstream, a separate call will generate the artefact
from this spec alone — seeing no workspace, no context, no broader
conversation. whatever the artefact should contain, avoid,
emphasise, or structure: you must make it an explicit spec item
here, or it will not appear.

## a few moves

before writing spec items, name the cached take. what would a sharp
person reach for as the obvious shape of the artefact? write it
down. now ask: does this shape actually serve the task as posed, or
is it a generic-artefact-shaped template? the load-bearing spec
items are the ones the generator wouldn't infer from the headline
alone — the *workspace-specific* findings, positions, and pitfalls.

attack each draft spec item by checking: would a generator with no
context know what to do with this? if the rule is "be clear" or
"discuss growth," it isn't a spec item — it's a placeholder. the
generator needs *prescription*, not gesture.

## what to produce

call `add_spec_item` once per rule. each spec item has:

- **headline** — a short, sharp label (10-15 words) naming the rule.
- **content** — one precise prescriptive statement about the
  artefact.

examples of good spec items:

- "the artefact should state that self-driving-car uptake in 2027
  will be substantially higher than in 2026, and anchor everything
  else to that framing."
- "walk through why prior estimates underweighted regulatory easing
  as a cause of acceleration."
- "recommend option A over option B, citing cost as the primary
  reason; don't leave the decision open."
- "name the 2024 benchmark result (roughly 37% on the held-out set)
  when describing current capability, rather than hedging with
  'substantial progress'."
- "the plan should name, for each step, who owns it and the trigger
  that starts it."
- "structure the document as numbered steps, not prose paragraphs."
- "avoid the phrase 'best practices' anywhere in the artefact; name
  the specific practice."
- "write in clear prose for a professional audience."

most spec items convey *content* — specific positions, findings,
claims, or framings the artefact must carry. structural and
stylistic rules matter too, but they're usually the minority.

## what makes a good spec

- **usually one rule per item — but don't be precious about it.**
  default to one rule per item; that keeps things easy to revise
  and supersede later. but if a rule is genuinely about a single
  coherent point that takes a few sentences to explain (with
  motivation, an example, or a nuance the generator needs to
  honour), one richer item beats two anaemic ones. if you find
  yourself writing a connecting "and" between unrelated points,
  that's two items.
- **prescriptive, not descriptive.** a spec item tells the artefact
  what to do — whether that's asserting something specific ("the
  artefact should state X"), committing to a position, structuring
  itself a particular way, or avoiding a failure mode. it's not a
  bare description of the world on its own; the artefact is where
  those descriptions land.
- **actionable by a generator with no context.** if a generator saw
  only your spec, would it know what shape the artefact takes? what
  specific things it should say? what style? what depth? what to
  leave out?
- **grounded in the workspace.** you have full workspace context.
  use it to surface rules that a generator could not infer from the
  artefact-task headline alone — specific findings the artefact
  must carry, positions already reached, known pitfalls,
  project-specific conventions, constraints the user has
  previously voiced.
- **specific.** "be clear" is not a spec item; "prefer 1-2 sentence
  paragraphs; never nest lists more than two levels" is. likewise,
  "discuss growth" is not a spec item; "state that 2027 growth
  will be substantially higher than 2026, and give at least one
  concrete reason" is.

## coverage

aim for a spec rich enough that, handed the spec alone, a capable
generator could produce a faithful first draft. this typically
means covering:

- **substantive content** — what specific claims, positions,
  findings, or recommendations must the artefact convey? what
  framing should it commit to? what should it explicitly *not* say?
- **shape and structure** — what kind of artefact is this, what
  sections or components must it have?
- **style and tone** — how should it read, what voice, what
  register?
- **anchors to the request** — what specific parts of the original
  request the artefact must directly serve?
- **known pitfalls** — failure modes the workspace suggests are
  worth explicitly guarding against.

err on the side of more spec items in cases where there's content
in the workspace that you think should make it into the artefact
(but it's okay if some content doesn't make it in!). the instance
writing the artefact will not have access to the workspace.

that said, keep the spec to a manageable size. **aim for roughly
10-20 items.** a spec with 40+ items is usually a sign of either
over-decomposing one rule into many narrow ones, or speculating
about content the workspace doesn't really push for. if you're
heading past that, prefer combining related items into single
richer ones, and drop items that aren't load-bearing.

## not your job

- you are **not** writing the artefact itself.
- you are **not** creating claims, questions, or judgements. only
  spec items via `add_spec_item`.
- you are **not** required to justify each spec item — the item's
  `content` field is the rule; keep it tight.
