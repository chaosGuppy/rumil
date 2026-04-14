# Global Prioritiser: Creation Phase

You have identified a cross-cutting research opportunity. Now create the question and dispatch research on it.

## Your Task

Use the `create_subquestion` tool to create a cross-cutting question. This tool creates a question and links it as a child of multiple parent questions in one operation.

### How to use `create_subquestion`

The key fields:

- **headline**: A clear, self-contained question (10-15 words). Must make sense without any prior context.
- **content**: Full explanation of why this question matters and what answering it would reveal.
- **links**: A list of parent question links. Each entry needs:
  - `parent_id`: Short ID of the parent question
  - `impact_on_parent_question`: 0-10 estimate of how much answering this question would help the parent
  - `reasoning`: Brief explanation of why this question matters for this parent
  - `role`: Usually `"structural"` (frames what to explore) or `"direct"` (directly answers the parent)
- **dispatches**: Research calls to queue on the new question. Include at least one dispatch to start investigation.

### Dispatch options

- `find_considerations`: General exploration. Set `max_rounds` (1-3) and `mode` (alternate/abstract/concrete).
- Scout variants: `scout_subquestions`, `scout_estimates`, `scout_deep_questions`, etc.
- `assess`: Synthesise considerations into a judgement. Budget cost: 1.
- `web_factcheck`: Look up a specific fact via web search. Budget cost: 1. Only for concrete factual questions.

### Requirements

- The question must link to **at least 2 parent questions** from different branches
- Set `impact_on_parent_question` honestly for each link -- higher for parents where the answer is more decision-relevant
- Dispatch at least one research call on the new question
- Keep total dispatch budget within bounds

## Budget Accounting

Each dispatch costs budget:
- find_considerations / scouts: up to `max_rounds`
- assess: exactly 1
- web_factcheck: exactly 1
