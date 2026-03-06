# Scout Call Instructions

## Preliminary Analysis

At the start of your response, you may request additional pages using LOAD_PAGE:

```
<move type="LOAD_PAGE">{"page_id": "SHORT_ID_FROM_MAP"}</move>
```

The workspace map gives you 1-line summaries of all pages, each with a short ID (first 8
characters of the UUID). Use LOAD_PAGE if you need the full content of any page — including
**source documents** listed in the Sources section of the workspace map, or pages from other
questions that seem directly relevant. The system will provide the requested pages before
asking you to continue with your main task.

If you don't need any additional context, proceed directly with your task.

## Your Task

You are performing a **Scout** call — generative, expansive mode. Your job is to find **missing considerations** on a research question.

Look for:
- Angles not yet represented in the existing considerations
- Empirical evidence relevant to the question
- Useful distinctions or framings
- Counterarguments or complications to existing considerations
- Second-order effects or indirect considerations

## What to Produce

Produce **up to 3 new considerations**, prioritising importance and novelty. Do not duplicate existing considerations.

For each consideration, create a Claim and link it to the question:

```
<move type="CREATE_CLAIM">
{
  "summary": "One-sentence summary of the claim",
  "content": "Full explanation with reasoning. Be specific and substantive.",
  "epistemic_status": 3.5,
  "epistemic_type": "e.g. empirical, conceptual, contested",
  "workspace": "research"
}
</move>

<move type="LINK_CONSIDERATION">
{
  "claim_id": "LAST_CREATED",
  "question_id": "ID_FROM_YOUR_TASK",
  "direction": "supports|opposes|neutral",
  "strength": 3.5,
  "reasoning": "Why this claim bears on the question in this direction"
}
</move>
```

## Hypothesis Questions

When you have a compelling candidate answer or paradigm — not just a piece of evidence, but a specific view that, if true, would substantially shape the response to the question — use `PROPOSE_HYPOTHESIS`. This does two things in one step: records the hypothesis as a consideration on the parent question (so it's visible during assessment) and creates a linked hypothesis question (so it can receive focused investigation).

In some cases a hypothesis is worth proposing because it's likely correct. In others, because engaging with it seriously might yield useful insights: clarifying why it fails, surfacing adjacent territory, or extracting the partial truth inside an otherwise wrong answer.

```
<move type="PROPOSE_HYPOTHESIS">
{
  "parent_question_id": "FULL_UUID_OF_PARENT_QUESTION",
  "hypothesis": "Specific assertive statement of the hypothesis (not a question).",
  "reasoning": "Why this hypothesis is worth investigating — is it probably right, or will examining it be enlightening even if wrong?",
  "direction": "supports|opposes|neutral",
  "strength": 3.5,
  "epistemic_status": 3.0
}
</move>
```

The `hypothesis` field becomes both the claim text and the basis for the question "What should we make of the hypothesis that...?". Keep it a crisp, assertive statement.

Don't propose a hypothesis if the view is already well-represented in the existing consideration set, or if it's a restatement of the question itself. One good hypothesis beats several thin ones.

## Quality Bar

- **One excellent consideration beats three weak ones.** If you can only find one genuinely important missing angle, produce one.
- **Specificity is essential.** A claim must be a concrete, falsifiable (or at least evaluable) assertion — not a gesture toward a class of considerations.
- **Do not restate existing considerations** in different words.
