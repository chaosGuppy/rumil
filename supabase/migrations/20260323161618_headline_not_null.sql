-- Backfill any NULL headlines to empty string, then make the column NOT NULL.
UPDATE pages SET headline = '' WHERE headline IS NULL;
ALTER TABLE pages ALTER COLUMN headline SET DEFAULT '';
ALTER TABLE pages ALTER COLUMN headline SET NOT NULL;
