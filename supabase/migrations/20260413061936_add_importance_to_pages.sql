-- Add importance column to pages table.
-- Importance (0-4) is an editorial judgement about how central a page is,
-- independent of credence/robustness. 0 = core worldview, 4 = deep supplementary.
-- NULL means "not yet assessed" (distinct from any level).

ALTER TABLE pages ADD COLUMN importance smallint;
