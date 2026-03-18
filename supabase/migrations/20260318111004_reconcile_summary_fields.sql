-- Step 1: Copy summary into summary_short where summary_short is empty.
UPDATE pages SET summary_short = summary WHERE summary_short = '';

-- Step 2: Rename summary_short -> headline, summary_medium -> abstract.
ALTER TABLE pages RENAME COLUMN summary_short TO headline;
ALTER TABLE pages RENAME COLUMN summary_medium TO abstract;

-- Step 3: Drop the old summary column.
ALTER TABLE pages DROP COLUMN summary;

-- Step 4: Rename embedding field_name from 'summary' to 'headline'.
UPDATE page_embeddings SET field_name = 'headline' WHERE field_name = 'summary';

-- Step 5: Rebuild match_pages to reflect the renamed columns.
-- Drop both the 6-param and 7-param overloads so we end up with a single function.
DROP FUNCTION IF EXISTS match_pages(
    extensions.vector, DOUBLE PRECISION, INTEGER, TEXT, UUID, TEXT
);
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
    filter_ab_run_id TEXT DEFAULT NULL
)
RETURNS TABLE(
    id TEXT,
    page_type TEXT,
    layer TEXT,
    workspace TEXT,
    content TEXT,
    headline TEXT,
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
