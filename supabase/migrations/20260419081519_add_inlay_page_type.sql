-- Register the INLAY page type and the INLAY_OF link type.
--
-- Inlay pages are model-authored UI fragments (HTML+CSS+JS blobs) that
-- render a question's content area through a sandboxed iframe instead
-- of the stock article view. Each inlay binds to exactly one target
-- (a question, or in future a project) via an INLAY_OF link. See
-- planning/inlay-ui.md for the full design and MVP scope.
--
-- The `pages.page_type` and `page_links.link_type` columns are plain
-- TEXT with no CHECK constraints or enum types, so no DDL is required
-- to let the new values land. This migration is a marker for the
-- schema history (and catches migration ordering bugs early) and adds
-- supporting indexes so that looking up a question's active inlays is
-- cheap.
--
-- Indexes:
--   idx_pages_type_inlay — partial index on inlay rows so scanning
--     "all inlays" doesn't page through the whole pages table.
--   idx_page_links_type_inlay_of — partial index on inlay_of links
--     by target, which is the lookup the /inlays endpoint performs
--     per question.

CREATE INDEX IF NOT EXISTS idx_pages_type_inlay
    ON pages (id)
    WHERE page_type = 'inlay';

CREATE INDEX IF NOT EXISTS idx_page_links_type_inlay_of
    ON page_links (to_page_id)
    WHERE link_type = 'inlay_of';
