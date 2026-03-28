-- Staged runs: event-sourced mutations + staged visibility

-- 1. mutation_events table for event-sourced superseding and link mutations
CREATE TABLE mutation_events (
    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    run_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('supersede_page', 'delete_link', 'change_link_role')),
    target_id TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_mutation_events_target ON mutation_events(target_id);
CREATE INDEX idx_mutation_events_run ON mutation_events(run_id);

ALTER TABLE mutation_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY "allow all" ON mutation_events FOR ALL USING (true) WITH CHECK (true);

-- 2. staged column on pages and page_links (replaces ab_run_id for visibility)
ALTER TABLE pages ADD COLUMN staged BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE page_links ADD COLUMN staged BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX idx_pages_staged ON pages(staged) WHERE staged = TRUE;
CREATE INDEX idx_page_links_staged ON page_links(staged) WHERE staged = TRUE;

-- 3. staged column on runs
ALTER TABLE runs ADD COLUMN staged BOOLEAN NOT NULL DEFAULT FALSE;

-- 4. Backfill: existing AB arm data becomes staged
UPDATE pages SET staged = TRUE WHERE ab_run_id IS NOT NULL;
UPDATE page_links SET staged = TRUE WHERE ab_run_id IS NOT NULL;
UPDATE runs SET staged = TRUE WHERE ab_run_id IS NOT NULL;

-- 5. Update match_pages RPC: replace ab_run_id filter with staged visibility
DROP FUNCTION IF EXISTS match_pages(
    extensions.vector, DOUBLE PRECISION, INTEGER, TEXT, UUID, TEXT, TEXT
);

CREATE OR REPLACE FUNCTION match_pages(
    query_embedding extensions.vector(1024),
    match_threshold DOUBLE PRECISION DEFAULT 0.5,
    match_count INTEGER DEFAULT 10,
    filter_workspace TEXT DEFAULT NULL,
    filter_project_id UUID DEFAULT NULL,
    filter_field_name TEXT DEFAULT NULL,
    filter_staged_run_id TEXT DEFAULT NULL
)
RETURNS TABLE(
    id TEXT,
    page_type TEXT,
    layer TEXT,
    workspace TEXT,
    content TEXT,
    headline TEXT,
    abstract TEXT,
    project_id UUID,
    epistemic_status DOUBLE PRECISION,
    epistemic_type TEXT,
    provenance_model TEXT,
    provenance_call_type TEXT,
    provenance_call_id TEXT,
    created_at TIMESTAMPTZ,
    superseded_by TEXT,
    is_superseded BOOLEAN,
    extra JSONB,
    run_id TEXT,
    field_name TEXT,
    similarity DOUBLE PRECISION
)
LANGUAGE sql STABLE
SET search_path = public, extensions
AS $$
    SELECT
        p.id,
        p.page_type,
        p.layer,
        p.workspace,
        p.content,
        p.headline,
        p.abstract,
        p.project_id,
        p.epistemic_status,
        p.epistemic_type,
        p.provenance_model,
        p.provenance_call_type,
        p.provenance_call_id,
        p.created_at,
        p.superseded_by,
        p.is_superseded,
        p.extra,
        p.run_id,
        pe.field_name,
        1 - (pe.embedding <=> query_embedding) AS similarity
    FROM page_embeddings pe
    JOIN pages p ON p.id = pe.page_id
    WHERE p.is_superseded = FALSE
      AND 1 - (pe.embedding <=> query_embedding) > match_threshold
      AND (filter_workspace IS NULL OR p.workspace = filter_workspace)
      AND (filter_project_id IS NULL OR p.project_id = filter_project_id)
      AND (filter_field_name IS NULL OR pe.field_name = filter_field_name)
      AND (filter_staged_run_id IS NULL
           OR p.staged = FALSE
           OR p.run_id = filter_staged_run_id)
    ORDER BY pe.embedding <=> query_embedding
    LIMIT match_count;
$$;

-- 6. Update get_root_questions RPC: replace ab_run_id filter with staged visibility
DROP FUNCTION IF EXISTS get_root_questions(TEXT);
DROP FUNCTION IF EXISTS get_root_questions(TEXT, UUID, TEXT);

CREATE OR REPLACE FUNCTION get_root_questions(
    ws TEXT,
    pid UUID DEFAULT NULL,
    p_staged_run_id TEXT DEFAULT NULL
)
RETURNS SETOF pages
LANGUAGE sql STABLE AS $$
    SELECT p.* FROM pages p
    WHERE p.page_type = 'question'
      AND p.workspace = ws
      AND p.is_superseded = FALSE
      AND (pid IS NULL OR p.project_id = pid)
      AND (p_staged_run_id IS NULL
           OR p.staged = FALSE
           OR p.run_id = p_staged_run_id)
      AND p.id NOT IN (
          SELECT to_page_id FROM page_links WHERE link_type = 'child_question'
      )
    ORDER BY p.created_at DESC;
$$;
