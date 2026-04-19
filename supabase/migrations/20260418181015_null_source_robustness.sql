-- Source pages no longer carry robustness — they report what an external
-- document says, not a judged view. Clear the baseline-1 score (and its
-- placeholder reasoning) from any existing source rows so renderers that
-- gate on `robustness IS NOT NULL` stop emitting misleading "R1/5" badges.

UPDATE pages
SET robustness = NULL, robustness_reasoning = NULL
WHERE page_type = 'source';
