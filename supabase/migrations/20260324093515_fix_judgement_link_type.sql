-- Fix judgement-to-question links that were incorrectly created as
-- 'consideration' instead of 'related'. The create_judgement move
-- previously used LinkType.CONSIDERATION for inline links; it should
-- have used LinkType.RELATED.
UPDATE page_links
SET link_type = 'related'
WHERE link_type = 'consideration'
  AND from_page_id IN (SELECT id FROM pages WHERE page_type = 'judgement')
  AND to_page_id IN (SELECT id FROM pages WHERE page_type = 'question');
