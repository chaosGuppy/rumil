## Wire-format constraint for pairwise judging

The harness extracts the pair's preference from your `finalize` answer by exact-string match against these seven labels:
  - A strongly preferred
  - A somewhat preferred
  - A slightly preferred
  - Approximately indifferent between A and B
  - B slightly preferred
  - B somewhat preferred
  - B strongly preferred

Your `finalize.answer` must end with one of these seven labels, verbatim, on its own line, with nothing after it.
