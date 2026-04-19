-- Enforce the 0-4 range for pages.importance (added in 20260413061936).
-- The 0-4 meaning was documented in a comment but not enforced. NULL
-- remains allowed ("not yet assessed").

ALTER TABLE pages ADD CONSTRAINT pages_importance_range
    CHECK (importance IS NULL OR (importance >= 0 AND importance <= 4));
