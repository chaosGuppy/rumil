-- View pages: structured summaries of current understanding on a question.
-- VIEW pages link to VIEW_ITEM pages via view_item links that carry
-- importance, section, and position metadata.

-- New columns on pages
ALTER TABLE pages ADD COLUMN sections TEXT[];   -- VIEW pages: ordered section names
ALTER TABLE pages ADD COLUMN meta_type TEXT;     -- VIEW_META pages: priority/annotation/proposal

-- New columns on page_links for VIEW_ITEM links
ALTER TABLE page_links ADD COLUMN importance SMALLINT;   -- 1-5 or NULL
ALTER TABLE page_links ADD COLUMN section TEXT;           -- section name
ALTER TABLE page_links ADD COLUMN "position" INTEGER;     -- order within section

ALTER TABLE page_links ADD CONSTRAINT importance_range
    CHECK (importance >= 1 AND importance <= 5);
