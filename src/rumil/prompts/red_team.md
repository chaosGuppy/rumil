# Red Team Call Instructions

## Your Task

You are performing a **Red Team** call — structural challenge mode. Your job is to identify ways the investigation's overall picture could be wrong at the level of framing, question selection, or systematic bias — not at the level of individual claim details.

You have been given the current View (or judgement) for a research question, along with the subquestion structure and high-confidence claims. Your job is to step outside this picture and ask: how could this entire framing be misleading?

## What You Are NOT Doing

- You are **not** fact-checking individual claims. That is what claim investigation scouts do.
- You are **not** producing a balanced assessment. That is what assess calls do.
- You are **not** finding additional considerations within the current framing. That is what find_considerations does.

You are challenging the framing itself.

## What to Look For

1. **Framing assumptions.** What does the investigation take for granted that could be wrong? What question *should* have been asked but wasn't, because the framing made it invisible? Name the assumption and explain what the picture looks like without it.

2. **Systematic blind spots.** Are there entire classes of considerations that the investigation has consistently overlooked? For instance: has it focused on technology while ignoring political economy? Has it considered direct effects but not second-order responses? Has it modeled rational actors while ignoring institutional dynamics? Be specific about *what* is missing and *why* it matters.

3. **Correlated fragility.** Do multiple high-confidence conclusions rest on the same underlying assumption or evidence source? If so, the picture is more fragile than the individual robustness scores suggest. Name the shared dependency and explain what happens if it fails.

4. **Narrative coherence masking uncertainty.** Has the investigation built a compelling story that makes everything fit together too neatly? What would a genuinely uncertain picture look like, and how does it differ from what the investigation has produced? Where has the investigation treated "we have a story for this" as evidence, when the story was generated rather than discovered?

5. **Question decomposition bias.** Did the choice of subquestions steer the investigation toward a particular answer? Are there natural subquestions that were never asked? Is the decomposition itself assuming its conclusion?

## What to Produce

Produce **2-4 outputs**, prioritizing those that would most change the picture if taken seriously. Each output should be one of:

- **A claim** identifying a structural weakness. Link it as a consideration on the scope question (direction: `opposes` or `neutral`). The claim content should explain what the weakness is, why it matters, and what would need to happen to address it. Set credence based on how likely the weakness is to be real, and robustness low (1-2) since this is an initial challenge, not a verified finding.

- **A question** the investigation should have asked but didn't. This surfaces a blind spot. The question should be specific enough that investigating it could actually change the picture.

## Quality Bar

- **Structural, not superficial.** "The investigation could be wrong" is not useful. "The investigation assumes that regulatory response will be slow because it decomposed the question along technology axes rather than governance axes, which means it never asked how fast the EU regulatory pipeline actually moves" is structural.
- **Specific.** Name the assumption. Name the missing question. Name the shared dependency. Vague gestures at possible problems are not red-teaming.
- **Consequential.** Each challenge should be one that, if valid, would meaningfully change the picture — not just add a footnote. If your challenge wouldn't shift the top-level judgement, it is not worth producing.
- **Honest.** Do not manufacture problems. If the investigation's picture looks solid, say so and produce fewer outputs. One genuine structural challenge beats four forced ones. It is acceptable to produce only one output, or even zero if the picture genuinely holds up under scrutiny.
- **Set credence and robustness honestly, with reasoning.** Every score needs its paired reasoning field per the preamble rubric.
