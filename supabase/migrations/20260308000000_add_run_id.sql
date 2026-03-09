-- Add run_id to all tables for per-run isolation.
-- This replaces the separate test schema with a single public schema
-- where each run (production or test) is identified by its run_id.

ALTER TABLE pages ADD COLUMN run_id TEXT NOT NULL DEFAULT '__legacy__';
ALTER TABLE page_links ADD COLUMN run_id TEXT NOT NULL DEFAULT '__legacy__';
ALTER TABLE calls ADD COLUMN run_id TEXT NOT NULL DEFAULT '__legacy__';
ALTER TABLE page_ratings ADD COLUMN run_id TEXT NOT NULL DEFAULT '__legacy__';
ALTER TABLE page_flags ADD COLUMN run_id TEXT NOT NULL DEFAULT '__legacy__';

-- Budget: replace single-row (id=1) design with per-run rows.
DROP TABLE budget;
CREATE TABLE budget (
    run_id TEXT PRIMARY KEY,
    total INTEGER NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
ALTER TABLE budget ENABLE ROW LEVEL SECURITY;
CREATE POLICY "allow all" ON budget FOR ALL USING (true) WITH CHECK (true);

-- Indexes for filtering by run_id.
CREATE INDEX idx_pages_run_id ON pages(run_id);
CREATE INDEX idx_page_links_run_id ON page_links(run_id);
CREATE INDEX idx_calls_run_id ON calls(run_id);
CREATE INDEX idx_page_ratings_run_id ON page_ratings(run_id);
CREATE INDEX idx_page_flags_run_id ON page_flags(run_id);

-- Drop old budget function signatures before recreating with new parameters.
-- PostgreSQL identifies functions by name + arg types, so CREATE OR REPLACE
-- with different parameters would create an overload, not a replacement.
DROP FUNCTION IF EXISTS consume_budget(INTEGER);
DROP FUNCTION IF EXISTS add_budget(INTEGER);

CREATE OR REPLACE FUNCTION consume_budget(rid TEXT, amount INTEGER)
RETURNS BOOLEAN
LANGUAGE plpgsql AS $$
DECLARE
    cur_total INTEGER;
    cur_used INTEGER;
BEGIN
    SELECT total, used INTO cur_total, cur_used FROM budget WHERE run_id = rid;
    IF cur_total IS NULL OR (cur_used + amount) > cur_total THEN
        RETURN FALSE;
    END IF;
    UPDATE budget SET used = used + amount WHERE run_id = rid;
    RETURN TRUE;
END;
$$;

CREATE OR REPLACE FUNCTION add_budget(rid TEXT, amount INTEGER)
RETURNS VOID
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE budget SET total = total + amount WHERE run_id = rid;
END;
$$;

-- Drop the test schema entirely.
DROP SCHEMA IF EXISTS test CASCADE;
