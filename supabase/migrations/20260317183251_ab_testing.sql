-- AB testing: runs table, ab_runs table, isolation columns

CREATE TABLE ab_runs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    project_id UUID NOT NULL REFERENCES projects(id),
    question_id TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_ab_runs_project ON ab_runs(project_id);

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    project_id UUID NOT NULL REFERENCES projects(id),
    question_id TEXT,
    config JSONB DEFAULT '{}',
    ab_run_id TEXT REFERENCES ab_runs(id),
    ab_arm TEXT CHECK (ab_arm IN ('a', 'b')),
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_runs_project ON runs(project_id);
CREATE INDEX idx_runs_ab_run ON runs(ab_run_id);

ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY "allow all" ON runs FOR ALL USING (true) WITH CHECK (true);
ALTER TABLE ab_runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY "allow all" ON ab_runs FOR ALL USING (true) WITH CHECK (true);

-- Isolation columns on existing tables
ALTER TABLE pages ADD COLUMN ab_run_id TEXT;
ALTER TABLE page_links ADD COLUMN ab_run_id TEXT;
CREATE INDEX idx_pages_ab_run ON pages(ab_run_id);
CREATE INDEX idx_page_links_ab_run ON page_links(ab_run_id);

-- Update match_pages RPC: add optional ab_run_id filter
DROP FUNCTION IF EXISTS match_pages(
    extensions.vector, DOUBLE PRECISION, INTEGER, TEXT, UUID, TEXT
);

CREATE OR REPLACE FUNCTION match_pages(
    query_embedding extensions.vector(1024),
    match_threshold DOUBLE PRECISION DEFAULT 0.5,
    match_count INTEGER DEFAULT 10,
    filter_workspace TEXT DEFAULT NULL,
    filter_project_id UUID DEFAULT NULL,
    filter_field_name TEXT DEFAULT NULL,
    filter_ab_run_id TEXT DEFAULT NULL
)
RETURNS TABLE(
    id TEXT,
    page_type TEXT,
    layer TEXT,
    workspace TEXT,
    content TEXT,
    summary TEXT,
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
LANGUAGE sql STABLE AS $$
    SELECT
        p.id,
        p.page_type,
        p.layer,
        p.workspace,
        p.content,
        p.summary,
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
      AND (filter_ab_run_id IS NULL
           OR p.ab_run_id IS NULL
           OR p.ab_run_id = filter_ab_run_id)
    ORDER BY pe.embedding <=> query_embedding
    LIMIT match_count;
$$;

-- Drop old get_root_questions overload to avoid ambiguity
DROP FUNCTION IF EXISTS get_root_questions(TEXT, UUID);

-- Update get_root_questions RPC: add optional ab_run_id filter
CREATE OR REPLACE FUNCTION get_root_questions(
    ws TEXT,
    pid UUID DEFAULT NULL,
    p_ab_run_id TEXT DEFAULT NULL
)
RETURNS SETOF pages
LANGUAGE sql STABLE AS $$
    SELECT p.* FROM pages p
    WHERE p.page_type = 'question'
      AND p.workspace = ws
      AND p.is_superseded = FALSE
      AND (pid IS NULL OR p.project_id = pid)
      AND (p_ab_run_id IS NULL
           OR p.ab_run_id IS NULL
           OR p.ab_run_id = p_ab_run_id)
      AND p.id NOT IN (
          SELECT to_page_id FROM page_links WHERE link_type = 'child_question'
      )
    ORDER BY p.created_at DESC;
$$;
