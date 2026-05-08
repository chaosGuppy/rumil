## Wire-format constraint for pairwise judging

The harness extracts the pair's preference from your `finalize` answer by exact-string match against these seven labels:
  - A strongly preferred
  - A somewhat preferred
  - A slightly preferred
  - Approximately indifferent between A and B
  - B slightly preferred
  - B somewhat preferred
  - B strongly preferred

Your `finalize.answer` MUST end with one of these seven labels, verbatim, on its own line, with nothing after it. The verdict subroutine will produce a label that satisfies this constraint; preserve it unchanged in the final answer. Do NOT substitute a different phrasing (`A clearly better`, `Preference: -2`, `A is the winner`, etc.) — those will not parse and the judgment will be discarded. If you genuinely disagree with the verdict's strength, your only acceptable move is to spawn the verdict subroutine again with a tightening intent and use its new label.
