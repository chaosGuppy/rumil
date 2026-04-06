-- Add fruit_remaining column to pages for judgements to record
-- how much useful investigation remains on the assessed question/claim.
ALTER TABLE pages ADD COLUMN fruit_remaining smallint;
