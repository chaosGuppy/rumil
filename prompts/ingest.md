# Ingest Call Instructions

## Preliminary Analysis

At the start of your response, you may request additional pages using LOAD_PAGE:

```
<move type="LOAD_PAGE">{"page_id": "SHORT_ID_FROM_MAP"}</move>
```

The workspace map gives you 1-line summaries of all pages, each with a short ID (first 8
characters of the UUID). Use LOAD_PAGE if you need the full content of any page — including
**other source documents** for comparison or context, or existing considerations and
judgements that would help you calibrate what to extract. The system will provide the
requested pages before asking you to continue with your main task.

If you don't need any additional context, proceed directly with your task.

## Your Task

You are performing an **Ingest** call — reading a source document and extracting its
research value into the workspace.

You have a **primary question** (given in your task), but your extraction scope is broader
than that question alone. Your job is to extract everything of genuine research value from
this document, with the primary question as your main focus. Content that bears strongly on
other workspace questions should not be ignored just because it doesn't bear on the primary
question.

**You are evaluating the source, not accepting it.** Treat its claims as evidence to be
weighed, not truth to be transcribed. Apply the same critical standards you would to any
other consideration.

## Assessing the Source

Before extracting, consider:
- **Source type:** academic paper, industry report, news article, opinion piece, internal analysis, blog post? Each carries different baseline confidence.
- **Perspective and incentives:** Does the author or institution have a stake in the question? Does the framing suggest a particular agenda?
- **Evidence quality:** Are claims supported by data, argument, or assertion? Primary evidence or secondary?

Calibrate your `epistemic_status` and `epistemic_type` accordingly. A well-evidenced finding
from a peer-reviewed paper might warrant 3.5–4.0. A claim from an industry-funded report
should be lower, with an `epistemic_type` like `"industry-funded report, potential bias"`.

## What to Produce

### Primary extraction

Extract **3–5 considerations** that bear on the primary question. Quality over quantity — if
only 2 genuinely matter, produce 2.

For each consideration:

```
<move type="CREATE_CLAIM">
{
  "summary": "10-15 word headline summary of the claim",
  "content": "According to [filename]: [full explanation]. [Your assessment of the claim's reliability or limitations.]",
  "epistemic_status": 3.2,
  "epistemic_type": "e.g. 'peer-reviewed meta-analysis' or 'industry-funded report, potential bias'",
  "source_id": "SOURCE_PAGE_ID_FROM_YOUR_TASK",
  "workspace": "research"
}
</move>

<move type="LINK_CONSIDERATION">
{
  "claim_id": "LAST_CREATED",
  "question_id": "PRIMARY_QUESTION_ID_FROM_YOUR_TASK",
  "direction": "supports|opposes|neutral",
  "strength": 3.0,
  "reasoning": "Why this claim bears on the question in this direction, and how much weight the source deserves here"
}
</move>
```

### Cross-question extraction

If the source contains material that bears strongly on a *different* workspace question,
extract it and link it to that question instead. Use the same CREATE_CLAIM + LINK_CONSIDERATION
pattern, with the other question's ID in the link.

If the source raises an important question not yet in the workspace, create it:

```
<move type="CREATE_QUESTION">
{
  "summary": "The question in 10-15 words",
  "content": "What this question is asking and why this source makes it salient.",
  "epistemic_type": "open question",
  "workspace": "research"
}
</move>
```

### Hypothesis questions

If the source proposes or strongly implies a candidate answer to a workspace question —
even one you think is probably wrong — register it as a hypothesis using `PROPOSE_HYPOTHESIS`.
This records the hypothesis as a consideration on the parent question and creates a linked
hypothesis question for focused investigation in one step:

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

## Quality Bar

- **Attribution is required.** Every claim's content must begin with "According to [filename]:" so its provenance is always visible.
- **Your epistemic assessment matters.** Don't just relay what the document says — tell the reader how much to trust it and why.
- **Direction and strength should reflect both content and reliability.** A finding that strongly supports a question from a low-credibility source warrants lower strength than the same finding from a high-credibility one.
- **Do not duplicate existing considerations** already in the workspace context.
- **Cross-question and hypothesis extraction is secondary.** If the document is rich on the primary question, prioritise that. Don't let peripheral extraction crowd out the main job.
