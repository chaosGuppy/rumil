-- Step 1: Copy summary into summary_short where summary_short is empty.
UPDATE pages SET summary_short = summary WHERE summary_short = '';

-- Step 2: Drop all SQL functions that depend on the pages table structure
-- BEFORE renaming/dropping columns. Postgres tracks column dependencies for
-- LANGUAGE sql functions, so the column changes would fail otherwise.
DROP FUNCTION IF EXISTS match_pages(
    extensions.vector, DOUBLE PRECISION, INTEGER, TEXT, UUID, TEXT
);
DROP FUNCTION IF EXISTS match_pages(
    extensions.vector, DOUBLE PRECISION, INTEGER, TEXT, UUID, TEXT, TEXT
);
DROP FUNCTION IF EXISTS get_root_questions(TEXT);
DROP FUNCTION IF EXISTS get_root_questions(TEXT, UUID);
DROP FUNCTION IF EXISTS get_root_questions(TEXT, UUID, TEXT);
DROP FUNCTION IF EXISTS pages_missing_embedding(TEXT, INTEGER, TEXT, UUID);

-- Step 3: Rename summary_short -> headline, summary_medium -> abstract.
ALTER TABLE pages RENAME COLUMN summary_short TO headline;
ALTER TABLE pages RENAME COLUMN summary_medium TO abstract;

-- Step 4: Drop the old summary column.
ALTER TABLE pages DROP COLUMN summary;

-- Step 5: Rename embedding field_name from 'summary' to 'headline'.
UPDATE page_embeddings SET field_name = 'headline' WHERE field_name = 'summary';

-- Step 6: Recreate match_pages with renamed columns and AB run filtering.
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

-- Step 7: Recreate get_root_questions with AB run filtering.
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

-- Step 8: Recreate pages_missing_embedding.
CREATE OR REPLACE FUNCTION pages_missing_embedding(
    p_field_name TEXT,
    p_limit INTEGER DEFAULT 50,
    p_workspace TEXT DEFAULT NULL,
    p_project_id UUID DEFAULT NULL
)
RETURNS SETOF pages
LANGUAGE sql STABLE AS $$
    SELECT p.*
    FROM pages p
    LEFT JOIN page_embeddings pe
        ON pe.page_id = p.id AND pe.field_name = p_field_name
    WHERE pe.id IS NULL
      AND p.is_superseded = FALSE
      AND (p_workspace IS NULL OR p.workspace = p_workspace)
      AND (p_project_id IS NULL OR p.project_id = p_project_id)
    LIMIT p_limit;
$$;
