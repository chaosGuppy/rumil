-- Reasoning is now a first-class part of every credence and robustness
-- score: pages store it alongside the numeric score, and subsequent
-- mutations carry it via mutation_events (fold-in migration follows).

ALTER TABLE pages
  ADD COLUMN credence_reasoning text,
  ADD COLUMN robustness_reasoning text;

-- Backfill from the latest epistemic_scores row per page. The reasoning
-- field in epistemic_scores is a single unified string; mirror it into
-- whichever of the new columns has a non-null score.
UPDATE pages p SET
  credence_reasoning = CASE WHEN es.credence IS NOT NULL THEN es.reasoning END,
  robustness_reasoning = CASE WHEN es.robustness IS NOT NULL THEN es.reasoning END
FROM (
  SELECT DISTINCT ON (page_id)
    page_id, reasoning, credence, robustness
  FROM epistemic_scores
  ORDER BY page_id, created_at DESC
) es
WHERE p.id = es.page_id;

-- Any page born with a credence or robustness score but never revised
-- has no recorded reasoning; stub it so downstream rendering never
-- encounters a numeric score without an accompanying explanation.
UPDATE pages SET credence_reasoning = '(no reasoning recorded)'
WHERE credence IS NOT NULL AND credence_reasoning IS NULL;
UPDATE pages SET robustness_reasoning = '(no reasoning recorded)'
WHERE robustness IS NOT NULL AND robustness_reasoning IS NULL;
