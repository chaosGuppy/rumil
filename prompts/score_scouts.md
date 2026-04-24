# Scout fruit scoring

You are estimating how much useful investigation remains in each of several scout types on a research question. A scout is a fan-out investigation kind — e.g. hypotheses, analogies, paradigm cases, fact-checks — that can be dispatched on the scope question.

## Your task

Score each scout type on one dimension:

- **fruit** (0-10): How much useful investigation would this scout type still yield right now, given the current state of research? Use the shared scale:
  - 0 = nothing more to add
  - 1-2 = close to exhausted
  - 3-4 = most angles covered
  - 5-6 = diminishing but real returns
  - 7-8 = substantial work remains
  - 9-10 = wide open with many unexplored angles

## What you see

For each scout type you are given:
- `call_type` — the scout's ID (use this exactly in the output).
- a short description of what the scout produces.
- `last_fruit` — the fruit score reported at the end of the most recent run of this scout on this question, or `null` if it has never run.

## How to think about it

- **When `last_fruit` is known**, treat it as a strong prior. Stay close to it unless the research has evolved in a way that changes the opportunity.
- **When `last_fruit` is null**, estimate from scratch: compare what this scout would produce against what the current view and subquestions already cover. If the view already addresses most of what this scout would yield, score low; if it would open genuinely new ground, score high.
- The scope question and view tell you what ground is already covered. Items in the view's "key uncertainties" or thin sections are signals that related scout types probably have remaining fruit.

## Output

Provide one entry per scout in `scores`. For each entry, include `call_type`, `fruit`, and a one-sentence `reasoning`. Do not skip any scout you were shown.
