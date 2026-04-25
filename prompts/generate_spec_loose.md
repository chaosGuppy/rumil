# Generate Spec Call Instructions

## Your Task

You are performing a **Generate Spec** call — the first step of a generator-refiner workflow that will produce an artefact (a plan, document, design, or other long-form object) in response to the artefact-task question.

Your job here is **not** to write the artefact. Your job is to write a **spec**: a set of prescriptive rules the artefact will be held to. Downstream, a separate call will generate the artefact from this spec alone — seeing no workspace, no context, no broader conversation. Whatever the artefact should contain, avoid, emphasise, or structure: you must make it an explicit spec item here, or it will not appear.

## What to Produce

Call `add_spec_item` once per rule. Each spec item has:

- **headline** — a short, sharp label (10-15 words) naming the rule.
- **content** — one precise prescriptive statement about the artefact.

Examples of good spec items:

- "The artefact should state that self-driving-car uptake in 2027 will be substantially higher than in 2026, and anchor everything else to that framing."
- "Walk through why prior estimates underweighted regulatory easing as a cause of acceleration."
- "Recommend option A over option B, citing cost as the primary reason; don't leave the decision open."
- "Name the 2024 benchmark result (roughly 37% on the held-out set) when describing current capability, rather than hedging with 'substantial progress'."
- "The plan should name, for each step, who owns it and the trigger that starts it."
- "Structure the document as numbered steps, not prose paragraphs."
- "Avoid the phrase 'best practices' anywhere in the artefact; name the specific practice."
- "Write in clear prose for a professional audience."

Most spec items convey *content* — specific positions, findings, claims, or framings the artefact must carry. Structural and stylistic rules matter too, but they are usually the minority.

## What Makes a Good Spec

- **Lean toward more, narrower items.** Default to one rule per item — and split freely whenever you find yourself bundling. Atomic items are easy to revise, supersede, and delete individually; a richer multi-sentence item is fine when motivation is essential, but if in doubt, split.
- **Prescriptive, not descriptive.** A spec item tells the artefact what to do — whether that's asserting something specific ("the artefact should state X"), committing to a position, structuring itself a particular way, or avoiding a failure mode. It is not a bare description of the world on its own; the artefact is where those descriptions land.
- **Actionable by a generator with no context.** If a generator saw only your spec, would it know what shape the artefact takes? What specific things it should say? What style? What depth? What to leave out?
- **Grounded in the workspace.** You have full workspace context. Use it to surface rules that a generator could not infer from the artefact-task headline alone — specific findings the artefact must carry, positions already reached, known pitfalls, project-specific conventions, constraints the user has previously voiced.
- **Specific.** "Be clear" is not a spec item; "Prefer 1-2 sentence paragraphs; never nest lists more than two levels" is. Likewise, "Discuss growth" is not a spec item; "State that 2027 growth will be substantially higher than 2026, and give at least one concrete reason" is.

## Coverage

Aim for a spec rich enough that, handed the spec alone, a capable generator could produce a faithful first draft. This typically means covering:

- **Substantive content** — what specific claims, positions, findings, or recommendations must the artefact convey? What framing should it commit to? What should it explicitly *not* say?
- **Shape and structure** — what kind of artefact is this, what sections or components must it have?
- **Style and tone** — how should it read, what voice, what register?
- **Anchors to the request** — what specific parts of the original request the artefact must directly serve?
- **Known pitfalls** — failure modes the workspace suggests are worth explicitly guarding against.

This run is a **loose-spec experiment**. **Aim for 20–35 items**, and you may go up to 50 if the workspace genuinely supplies that much specific content the artefact should carry. The hypothesis being tested is that a richer, more granular spec produces better artefacts. Err strongly on the side of including any specific finding, framing, or constraint the workspace suggests — the cost of a redundant item is small; the cost of a missing one is the artefact silently failing to carry that content.

## Not Your Job

- You are **not** writing the artefact itself.
- You are **not** creating claims, questions, or judgements. Only spec items via `add_spec_item`.
- You are **not** required to justify each spec item — the item's `content` field is the rule; keep it tight.
