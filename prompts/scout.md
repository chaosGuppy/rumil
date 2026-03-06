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

As well as considerations, you might want to register potential answers as hypothesis questions for further investigation. Hypotheses represent possible paradigm answers. In some cases a hypothesis question is worth creating because there is a decent chance that it is correct. In other cases, it is worth creating not because you think it's likely correct, but because engaging with it seriously might yield useful insights: clarifying why it fails, surfacing adjacent territory, or extracting the partial truth inside an otherwise wrong answer.

Frame hypothesis questions as: **"What should we make of the hypothesis that X?"**

In the question content, explain why you hope that investigation of this hypothesis might be helpful — is it mostly because you want a truth-assessment of the hypothesis, or mostly because you think understanding the dynamics around what X is getting right or wrong might be enlightening.

```
<move type="CREATE_QUESTION">
{
  "summary": "What should we make of the hypothesis that X?",
  "content": "Full statement of the hypothesis and why investigating it seems productive.",
  "epistemic_type": "hypothesis — speculative candidate answer",
  "workspace": "research",
  "hypothesis": true
}
</move>

<move type="LINK_CHILD_QUESTION">
{
  "from_page_id": "PARENT_QUESTION_ID",
  "to_page_id": "LAST_CREATED",
  "reasoning": "Hypothesis worth exploring"
}
</move>
```

Don't create a hypothesis question if the hypothesis is already well-represented in the existing consideration set, or if it's just a restatement of the question itself. One good hypothesis beats several thin ones.

## Quality Bar

- **One excellent consideration beats three weak ones.** If you can only find one genuinely important missing angle, produce one.
- **Specificity is essential.** A claim must be a concrete, falsifiable (or at least evaluable) assertion — not a gesture toward a class of considerations.
- **Do not restate existing considerations** in different words.
