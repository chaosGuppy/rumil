# Reassess Claims

You are reassessing one or more workspace claims together. You will be given the claims to reassess, context pages that inform the reassessment (e.g. new judgements on subquestions, fresh evidence), surrounding workspace context, and optionally free-text guidance explaining what the reassessment should achieve.

## Instructions

Produce one or more replacement claims. You may:
- **Update** a claim individually (one new claim superseding one old claim)
- **Merge** conflicting or redundant claims into a single coherent claim (one new claim superseding multiple old claims)
- **Split** a claim that covers distinct points (multiple new claims, each superseding the original)
- **Replace** a claim entirely if the context pages contradict it

Each replacement claim must be:
- **Standalone and self-contained.** The reader will never see the old claims. Do not reference "the prior version," "previously estimated," "updated to reflect," or any comparative language. Write as if from scratch.
- **Accurate.** Reflect what the evidence and context pages actually say. If context pages contradict a claim, the replacement must reflect the corrected information.
- **Well-sourced.** Reference source page IDs using `[page_id]` notation where appropriate.

Do NOT describe claims as "confirmed", "verified", or "empirically grounded" — let citations and credence/robustness speak for themselves. Every replacement claim needs fresh `credence_reasoning` and `robustness_reasoning` per the preamble rubric, reflecting the reassessment's current view (not the old claim's).

## Supersession

Each new claim has a `supersedes` field: a list of 8-char short IDs of old claims it replaces. Consideration links are automatically copied from superseded claims to the new claim, so the new claim inherits the old claims' positions in the graph.

- If you are updating a single claim, set `supersedes` to that claim's ID.
- If you are merging N claims, set `supersedes` to all N IDs.
- If you are splitting a claim into parts, each part should supersede the original.
- If you are adding an entirely new claim (not replacing anything), leave `supersedes` empty.

## Link operations

You may also specify link operations to restructure how claims connect to questions:

- **link_adds**: Create new consideration links between your new claims and questions. Each entry specifies `claim_index` (0-based index into your claims list), `question_id`, `strength` (0-5), and `reasoning`. Use this when a new or merged claim should bear on a question that wasn't linked to the old claims, or when the automatically-copied links don't capture the right relationship.
- **link_removals**: Remove existing links by their 8-char short ID. Use this to clean up links that no longer make sense after the reassessment — e.g. a copied link that points to a question the new claim no longer bears on.

## Using context pages

The "in light of" context pages are the primary driver of this reassessment. They typically contain:
- **Judgements** from subquestions that were investigated to resolve tension between claims
- **Evidence** that bears on the claims being reassessed

Read them carefully and let them inform your updated claims. The guidance text (if provided) explains what the reassessment should achieve — e.g. "reconcile these conflicting claims about X in light of the subquestion findings."

## What not to change

- Do not alter aspects of claims that are unrelated to the context pages or guidance.
- Do not invent information not supported by the evidence presented.
- Preserve each claim's general scope and purpose unless the context pages clearly warrant a change in scope.
