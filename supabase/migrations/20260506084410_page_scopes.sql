-- Page scopes: optional per-page (and per-link) restriction to a single
-- question. A row tagged with `scope_question_id = Q` is only visible to
-- callers whose DB instance is forked into matching scope; an untagged
-- row (NULL) is visible to all callers.
--
-- This is the same shape as the staged-runs visibility mechanism, with
-- one difference: the filter is opt-in. A DB with `scope_question_id IS
-- NULL` (the default) sees everything; a DB scoped to Q sees rows where
-- `scope_question_id IS NULL OR scope_question_id = Q`.
--
-- Discovery RPCs (match_pages, get_root_questions) gain a
-- filter_scope_question_id parameter; existing visibility predicates
-- (staged, hidden) are preserved verbatim.

ALTER TABLE pages ADD COLUMN scope_question_id TEXT REFERENCES pages(id);
ALTER TABLE page_links ADD COLUMN scope_question_id TEXT REFERENCES pages(id);
CREATE INDEX idx_pages_scope ON pages(scope_question_id) WHERE scope_question_id IS NOT NULL;
CREATE INDEX idx_page_links_scope ON page_links(scope_question_id) WHERE scope_question_id IS NOT NULL;

DROP FUNCTION IF EXISTS match_pages(
    extensions.vector, DOUBLE PRECISION, INTEGER, TEXT, UUID, TEXT, TEXT, BOOLEAN
);

CREATE OR REPLACE FUNCTION match_pages(
    query_embedding extensions.vector(1024),
    match_threshold DOUBLE PRECISION DEFAULT 0.5,
    match_count INTEGER DEFAULT 10,
    filter_workspace TEXT DEFAULT NULL,
    filter_project_id UUID DEFAULT NULL,
    filter_field_name TEXT DEFAULT NULL,
    filter_staged_run_id TEXT DEFAULT NULL,
    filter_include_hidden BOOLEAN DEFAULT false,
    filter_scope_question_id TEXT DEFAULT NULL
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
      AND (p.staged = FALSE OR p.run_id = filter_staged_run_id)
      AND (filter_include_hidden OR NOT p.hidden)
      AND (filter_scope_question_id IS NULL
           OR p.scope_question_id IS NULL
           OR p.scope_question_id = filter_scope_question_id)
    ORDER BY pe.embedding <=> query_embedding
    LIMIT match_count;
$$;

DROP FUNCTION IF EXISTS get_root_questions(TEXT, UUID, TEXT, BOOLEAN);

CREATE OR REPLACE FUNCTION get_root_questions(
    ws TEXT,
    pid UUID DEFAULT NULL,
    p_staged_run_id TEXT DEFAULT NULL,
    p_include_hidden BOOLEAN DEFAULT false,
    p_scope_question_id TEXT DEFAULT NULL
)
RETURNS SETOF pages
LANGUAGE sql STABLE AS $$
    SELECT p.* FROM pages p
    WHERE p.page_type = 'question'
      AND p.workspace = ws
      AND p.is_superseded = FALSE
      AND (pid IS NULL OR p.project_id = pid)
      AND (p.staged = FALSE OR p.run_id = p_staged_run_id)
      AND (p_include_hidden OR NOT p.hidden)
      AND (p_scope_question_id IS NULL
           OR p.scope_question_id IS NULL
           OR p.scope_question_id = p_scope_question_id)
      AND p.id NOT IN (
          SELECT to_page_id FROM page_links WHERE link_type = 'child_question'
      )
    ORDER BY p.created_at DESC;
$$;
