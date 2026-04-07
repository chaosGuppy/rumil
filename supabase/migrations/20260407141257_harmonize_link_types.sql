-- Harmonize link type semantics.
--
-- Before this migration, LinkType.CONSIDERATION was overloaded to mean both:
--   (1) "claim bears on question" (the canonical sense), and
--   (2) "this claim/judgement's conclusions rest on that other claim"
--       (the dependency sense).
--
-- After this migration:
--   - 'consideration' links are strictly claim -> question.
--   - claim<->claim and judgement<->claim 'consideration' links are rewritten
--     to 'depends_on' with the endpoints swapped, so that the resulting
--     direction matches the depends_on convention (from = dependent,
--     to = dependency). The pre-migration convention had the inverse
--     direction: from = sub-claim ("bearing on") -> to = parent-claim, which
--     translates to "parent depends on sub".
--   - The `direction` column is meaningless for depends_on; we drop it on
--     rewritten rows.
--
-- This migration writes directly to base rows without recording mutation_events.
-- That is acceptable here only because no staged runs are in flight at the
-- time of this migration; staged-run event replay will not reproduce the
-- pre-migration state for these rows.

UPDATE page_links pl
SET link_type = 'depends_on',
    from_page_id = pl.to_page_id,
    to_page_id = pl.from_page_id,
    direction = NULL
WHERE pl.link_type = 'consideration'
  AND pl.from_page_id IN (
      SELECT id FROM pages WHERE page_type IN ('claim', 'judgement')
  )
  AND pl.to_page_id IN (
      SELECT id FROM pages WHERE page_type IN ('claim', 'judgement')
  );

-- Any remaining 'consideration' link whose endpoints are not (claim/judgement
-- -> question) is anomalous under the new semantics. Fold those into 'related'
-- so the new invariants hold without losing graph connectivity.
UPDATE page_links pl
SET link_type = 'related',
    direction = NULL
WHERE pl.link_type = 'consideration'
  AND (
      pl.from_page_id NOT IN (
          SELECT id FROM pages WHERE page_type IN ('claim', 'judgement')
      )
      OR pl.to_page_id NOT IN (
          SELECT id FROM pages WHERE page_type = 'question'
      )
  );
