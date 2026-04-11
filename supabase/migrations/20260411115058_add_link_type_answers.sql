-- Introduce a dedicated LinkType.ANSWERS for judgement -> question links.
--
-- Previously, judgement -> question links were stored as 'related'. That
-- overloaded 'related' to mean both "general relation" and "this judgement
-- is the current answer to the question", which is a substantive enough
-- relation to deserve its own link type. It also caused a subtle bug in
-- the experimental orchestrator's _is_new_question check: inline citations
-- in a fresh question's content were being auto-linked as 'consideration',
-- making the question look like it already had research and causing phase
-- 1 to be incorrectly skipped.
--
-- After this migration:
--   - 'answers' links (judgement -> question) are first-class.
--   - 'related' means a general relation only.
--
-- This migration writes directly to base rows without recording mutation_events.
-- That is acceptable only because no staged runs are in flight at the time
-- of this migration; staged-run event replay will not reproduce the
-- pre-migration state for these rows.

UPDATE page_links pl
SET link_type = 'answers'
WHERE pl.link_type = 'related'
  AND pl.from_page_id IN (
      SELECT id FROM pages WHERE page_type = 'judgement'
  )
  AND pl.to_page_id IN (
      SELECT id FROM pages WHERE page_type = 'question'
  );
