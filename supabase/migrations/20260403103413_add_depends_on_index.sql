-- Partial index for efficient reverse lookups on DEPENDS_ON links.
-- Supports "what depends on page X?" queries for update propagation.
CREATE INDEX idx_page_links_depends_on_to
  ON page_links(to_page_id)
  WHERE link_type = 'depends_on';
