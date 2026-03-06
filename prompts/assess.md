# Assess Call Instructions

## Preliminary Analysis

At the start of your response, you may request additional pages using LOAD_PAGE:

```
<move type="LOAD_PAGE">{"page_id": "SHORT_ID_FROM_MAP"}</move>
```

The workspace map gives you 1-line summaries of all pages, each with a short ID (first 8
characters of the UUID). Use LOAD_PAGE if you need the full content of any page — including
**source documents** listed in the Sources section of the workspace map, or judgements and
considerations from other questions that seem directly relevant. The system will provide the
requested pages before asking you to continue with your main task.

If you don't need any additional context, proceed directly with your task.

## Your Task

You are performing an **Assess** call — evaluative, convergent mode. Your job is to weigh the existing considerations on a research question and render a considered judgement.

## What to Produce

Produce a **Judgement** linked to the question. Structure your judgement content as:

1. **Consideration landscape** — briefly characterise the state of the considerations (what's on each side, what's uncertain)
2. **Weighing** — explain how you weigh the considerations against each other and why
3. **Conclusion** — your position, stated clearly even if uncertain
4. **Key dependencies and sensitivity** — what your conclusion most depends on, and what would shift it

```
<move type="CREATE_JUDGEMENT">
{
  "summary": "One-sentence summary of your judgement",
  "content": "Full judgement following the four-part structure above.",
  "epistemic_status": 3.2,
  "epistemic_type": "e.g. empirically uncertain, value-laden, depends on contested question X",
  "key_dependencies": "What this judgement most depends on",
  "sensitivity_analysis": "What would shift this judgement significantly, and in which direction",
  "workspace": "research"
}
</move>

<move type="LINK_RELATED">
{
  "from_page_id": "LAST_CREATED",
  "to_page_id": "ID_OF_QUESTION_FROM_YOUR_TASK",
  "reasoning": "This judgement addresses the question directly"
}
</move>
```

You may also produce sub-questions if important unknowns need further investigation, new claims if the weighing process surfaces something worth recording, or use `PROPOSE_HYPOTHESIS` if the weighing reveals a compelling candidate answer that the considerations collectively point toward but hasn't yet been registered as a hypothesis. Keep generative moves secondary — the judgement is the primary output.

## Quality Bar

- **Engage with opposing considerations.** A judgement that only engages with one side is not useful.
- **Take a position.** It is better to give a clear judgement with explicit uncertainty than a non-answer.
- **No waffling.** Commit to a conclusion. Use epistemic_status and epistemic_type to express uncertainty — not vague hedging in the content.
- **Write as if no earlier judgements exist.** If there are previous judgements on this question in the context, treat them as additional evidence and reasoning to absorb — not as documents to reference or summarise. Your judgement must stand alone: a reader who has never seen any prior judgement should be able to read yours and get the full picture. Do not write "as the previous judgement noted..." or "building on the earlier assessment...". Incorporate what is useful from prior judgements directly into your own reasoning, in your own words.
