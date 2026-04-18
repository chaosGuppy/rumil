-- Scrub credence from epistemic_scores rows whose page is not a claim.
-- Under the new invariant only claims carry credence; stale historical
-- credence on judgements/summaries/wikis/sources/view_items would be
-- resurrected by apply_epistemic_overrides() on every page read.

-- Delete rows that would become empty (only credence, no robustness).
DELETE FROM epistemic_scores es
USING pages p
WHERE es.page_id = p.id
  AND p.page_type != 'claim'
  AND es.credence IS NOT NULL
  AND es.robustness IS NULL;

-- Null credence on rows that still carry a robustness score.
UPDATE epistemic_scores es
SET credence = NULL
FROM pages p
WHERE es.page_id = p.id
  AND p.page_type != 'claim'
  AND es.credence IS NOT NULL;
