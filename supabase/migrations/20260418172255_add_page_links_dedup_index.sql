-- Composite index supporting dedup lookups in save_link.
-- The dedup check queries by (from_page_id, to_page_id, link_type) before
-- inserting a new PageLink. Without a composite index, this hits
-- idx_page_links_from and then filters in-memory, which grows linear in
-- the fan-out of a source page. Adding a composite index makes the lookup
-- O(log n) per call regardless of fan-out.
CREATE INDEX IF NOT EXISTS idx_page_links_dedup
    ON page_links (from_page_id, to_page_id, link_type);
