-- Convert page.extra['source_id'] metadata into proper CITES links,
-- then remove the source_id key from extra.

INSERT INTO page_links (id, from_page_id, to_page_id, link_type, strength, reasoning)
SELECT
    gen_random_uuid()::text,
    p.id,
    p.extra->>'source_id',
    'cites',
    2.5,
    'Migrated from page.extra.source_id'
FROM pages p
WHERE p.extra->>'source_id' IS NOT NULL
  AND EXISTS (SELECT 1 FROM pages s WHERE s.id = p.extra->>'source_id')
ON CONFLICT DO NOTHING;

UPDATE pages
SET extra = extra - 'source_id'
WHERE extra->>'source_id' IS NOT NULL;
