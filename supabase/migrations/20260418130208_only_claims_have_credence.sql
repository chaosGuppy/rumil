-- Only claim pages carry a credence score.
--
-- Historically credence was set on judgements, summaries, wikis, sources,
-- and view items, where asking "how likely is this to be true?" doesn't
-- fit (judgements carry robustness; the others aren't positive truth-apt
-- assertions). This migration nulls out credence on non-claim pages and
-- adds a CHECK constraint to keep things honest going forward.
--
-- The epistemic_scores history is intentionally left intact for its
-- existing rows, but the NOT NULL constraints on credence/robustness are
-- dropped so that future rows can update just one dimension (e.g. raise
-- a judgement's robustness without touching credence).

UPDATE pages
SET credence = NULL
WHERE page_type != 'claim' AND credence IS NOT NULL;

ALTER TABLE pages DROP CONSTRAINT IF EXISTS credence_claim_only;
ALTER TABLE pages
  ADD CONSTRAINT credence_claim_only
  CHECK (credence IS NULL OR page_type = 'claim');

ALTER TABLE epistemic_scores ALTER COLUMN credence DROP NOT NULL;
ALTER TABLE epistemic_scores ALTER COLUMN robustness DROP NOT NULL;
ALTER TABLE epistemic_scores
  DROP CONSTRAINT IF EXISTS epistemic_scores_at_least_one_score;
ALTER TABLE epistemic_scores
  ADD CONSTRAINT epistemic_scores_at_least_one_score
  CHECK (credence IS NOT NULL OR robustness IS NOT NULL);
