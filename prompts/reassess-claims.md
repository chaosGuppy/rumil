# Reassess Claims

You're rewriting one or more workspace claims together in light of new evidence. Inputs: the claims to reassess, context pages that drive the reassessment (new judgements on subquestions, fresh evidence), surrounding workspace context, and optional free-text guidance naming what the reassessment should achieve.

## Options

Each replacement can:

- **Update** — one new claim supersedes one old claim.
- **Merge** — one new claim supersedes multiple old claims (conflicting or redundant).
- **Split** — multiple new claims each supersede the original (covers distinct points).
- **Replace** — an entirely new claim when context contradicts the old.

Each replacement claim must be:

- **Standalone.** The reader will never see the old claims. No "the prior version", "previously estimated", "updated to reflect", or any comparative language. Write as if from scratch.
- **Accurate to the evidence.** If context pages contradict a claim, the replacement reflects the corrected information.
- **Sourced.** Reference source page IDs inline with `[page_id]`.

Do not describe claims as "confirmed", "verified", or "empirically grounded" — let citations and credence/robustness carry that signal.

Headline discipline: the new headline must be no stronger than the weakest caveat in the new body.

## Supersession

Each new claim has a `supersedes` field: the 8-char short IDs of the old claims it replaces. Consideration links automatically copy from superseded claims to the new claim, so the new claim inherits the graph position.

- Updating one claim → `supersedes` is that claim's ID.
- Merging N claims → `supersedes` is all N IDs.
- Splitting one claim into parts → each part supersedes the original.
- Entirely new claim (not replacing anything) → leave `supersedes` empty.

## Link operations

You can also restructure how claims connect to questions:

- **link_adds** — new consideration links. Entry: `claim_index` (0-based into your claims list), `question_id`, `strength` (0–5), `reasoning`. Use when a new or merged claim should bear on a question the old claims didn't link to, or when the auto-copied links don't capture the right relationship.
- **link_removals** — remove existing links by 8-char short ID. Use to clean up links that no longer make sense after the reassessment (e.g. a copied link pointing at a question the new claim no longer bears on).

## Using context pages

The "in light of" pages are the primary driver. They typically contain:

- **Judgements** from subquestions investigated to resolve tension between claims.
- **Evidence** that bears on the claims being reassessed.

Read them carefully; let them drive the replacements. The guidance text (when provided) names what the reassessment should achieve — e.g. "reconcile these conflicting claims about X in light of the subquestion findings".

## What not to change

- Don't alter aspects of claims unrelated to the context pages or guidance.
- Don't invent information beyond what the evidence supports.
- Preserve each claim's general scope and purpose unless context clearly warrants a scope change.
