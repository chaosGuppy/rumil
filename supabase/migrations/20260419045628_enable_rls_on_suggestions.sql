-- Enable RLS on the suggestions table (added in 20260413062500).
-- Every other table in the project has RLS on with no policies;
-- suggestions was missed. See CLAUDE.md: service_role bypasses RLS, so
-- this locks out anon/authenticated while leaving backend access intact.

ALTER TABLE suggestions ENABLE ROW LEVEL SECURITY;
