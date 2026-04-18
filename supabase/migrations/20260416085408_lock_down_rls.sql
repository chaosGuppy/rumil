-- Lock down Row-Level Security across all tables.
--
-- SECURITY MODEL:
-- All backend access (CLI, API, orchestrators) uses the service_role key,
-- which has bypassrls privilege and is completely unaffected by RLS.
-- The frontend anon key is used ONLY for Realtime broadcast subscriptions
-- (not postgres_changes), so it never queries tables directly.
--
-- With RLS enabled and no policies, PostgreSQL's default behavior is to
-- deny all access for non-bypassrls roles (anon, authenticated).
-- This is the desired posture: the anon key cannot read or write any data.
--
-- If you need to add a new access path that does NOT use service_role,
-- you must add explicit RLS policies for the relevant tables.

-- 1. Enable RLS on the 5 tables that don't have it yet.
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE page_embeddings ENABLE ROW LEVEL SECURITY;
ALTER TABLE call_llm_exchanges ENABLE ROW LEVEL SECURITY;
ALTER TABLE epistemic_scores ENABLE ROW LEVEL SECURITY;
ALTER TABLE ab_eval_reports ENABLE ROW LEVEL SECURITY;

-- 2. Drop the "allow all" policies on the 10 tables that have them.
--    With these removed and no replacement policies, non-bypassrls roles
--    are implicitly denied all access (PostgreSQL default).
DROP POLICY IF EXISTS "allow all" ON pages;
DROP POLICY IF EXISTS "allow all" ON page_links;
DROP POLICY IF EXISTS "allow all" ON calls;
DROP POLICY IF EXISTS "allow all" ON budget;
DROP POLICY IF EXISTS "allow all" ON page_ratings;
DROP POLICY IF EXISTS "allow all" ON page_flags;
DROP POLICY IF EXISTS "allow all" ON call_sequences;
DROP POLICY IF EXISTS "allow all" ON runs;
DROP POLICY IF EXISTS "allow all" ON ab_runs;
DROP POLICY IF EXISTS "allow all" ON mutation_events;
