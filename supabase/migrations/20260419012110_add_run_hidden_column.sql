-- Add a `hidden` flag to the runs table so the TraceView run-picker can
-- filter out noise (smoke tests, failed experiments, etc.) without deleting
-- any data. Matches the analogous `projects.hidden` column.
--
-- Default FALSE so existing runs stay visible. NOT NULL to keep read-path
-- logic simple.
ALTER TABLE runs ADD COLUMN hidden BOOLEAN NOT NULL DEFAULT FALSE;
