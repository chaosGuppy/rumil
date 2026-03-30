-- Add credence (1-9) and robustness (1-5) columns to pages,
-- replacing the old epistemic_status/epistemic_type fields.

ALTER TABLE pages ADD COLUMN credence INTEGER DEFAULT 5;
ALTER TABLE pages ADD COLUMN robustness INTEGER DEFAULT 1;
ALTER TABLE pages ADD CONSTRAINT credence_range CHECK (credence >= 1 AND credence <= 9);
ALTER TABLE pages ADD CONSTRAINT robustness_range CHECK (robustness >= 1 AND robustness <= 5);

-- Append-only table for epistemic score updates (like page_ratings).
-- Latest row per page_id wins at read time.
CREATE TABLE epistemic_scores (
    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    page_id TEXT NOT NULL REFERENCES pages(id),
    call_id TEXT NOT NULL REFERENCES calls(id),
    credence INTEGER NOT NULL CHECK (credence >= 1 AND credence <= 9),
    robustness INTEGER NOT NULL CHECK (robustness >= 1 AND robustness <= 5),
    reasoning TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id TEXT NOT NULL DEFAULT '__legacy__'
);
CREATE INDEX idx_epistemic_scores_page_id ON epistemic_scores(page_id);
CREATE INDEX idx_epistemic_scores_run_id ON epistemic_scores(run_id);

-- Recreate match_pages with new columns
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
    credence INTEGER,
    robustness INTEGER,
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
        p.credence,
        p.robustness,
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
