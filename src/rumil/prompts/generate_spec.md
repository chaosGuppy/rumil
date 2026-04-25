# Generate Spec Call Instructions

## Your Task

You are performing a **Generate Spec** call — the first step of a generator-refiner workflow that will produce an artefact (a plan, document, design, or other long-form object) in response to the artefact-task question.

Your job here is **not** to write the artefact. Your job is to write a **spec**: a set of atomic prescriptive rules the artefact will be held to. Downstream, a separate call will generate the artefact from this spec alone — seeing no workspace, no context, no broader conversation. Whatever the artefact should contain, avoid, emphasise, or structure: you must make it an explicit spec item here, or it will not appear.

## What to Produce

Call `add_spec_item` once per rule. Each spec item has:

- **headline** — a short, sharp label (10-15 words) naming the rule.
- **content** — one precise prescriptive statement about the artefact.

Examples of good spec items:

- "The plan should name, for each step, who owns it and the trigger that starts it."
- "Avoid the phrase 'best practices' anywhere in the artefact; name the specific practice."
- "Include at least one concrete worked example for each novel concept introduced."
- "Structure the document as numbered steps, not prose paragraphs."
- "Cite a source for every quantitative claim."

## What Makes a Good Spec

- **Atomic.** One rule per spec item. If you find yourself writing "and", consider whether you have two items.
- **Prescriptive, not descriptive.** Spec items are about the artefact, not about the world. "The artefact should X" rather than "X is true".
- **Actionable by a generator with no context.** If a generator saw only your spec, would it know what shape the artefact takes? What content? What style? What depth? What to leave out?
- **Grounded in the workspace.** You have full workspace context. Use it to surface rules that a generator could not infer from the artefact-task headline alone — relevant prior considerations, known pitfalls, project-specific conventions, constraints the user has previously voiced.
- **Specific.** "Be clear" is not a spec item. "Prefer 1-2 sentence paragraphs; never nest lists more than two levels" is.

## Coverage

Aim for a spec rich enough that, handed the spec alone, a capable generator could produce a faithful first draft. This typically means covering:

- **Shape and structure** — what kind of artefact is this, what sections or components must it have?
- **Content** — what must be included, what must be excluded, what must be addressed?
- **Style and tone** — how should it read, what voice, what register?
- **Anchors to the request** — what specific parts of the original request the artefact must directly serve?
- **Known pitfalls** — failure modes the workspace suggests are worth explicitly guarding against.

Err on the side of more spec items. The downstream refinement loop can prune or supersede what doesn't hold up; it cannot invent what isn't there.

## Not Your Job

- You are **not** writing the artefact itself.
- You are **not** creating claims, questions, or judgements. Only spec items via `add_spec_item`.
- You are **not** required to justify each spec item — the item's `content` field is the rule; keep it tight.
