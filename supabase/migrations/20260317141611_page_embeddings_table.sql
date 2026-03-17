-- Separate embeddings table: one row per (page, field) pair.
-- Replaces the embedding column on pages.

CREATE TABLE page_embeddings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    page_id TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    embedding extensions.vector(1024) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (page_id, field_name)
);

CREATE INDEX idx_page_embeddings_vector ON page_embeddings
    USING ivfflat (embedding extensions.vector_cosine_ops)
    WITH (lists = 100);

CREATE INDEX idx_page_embeddings_page_id ON page_embeddings (page_id);

-- Migrate existing embeddings from pages table
INSERT INTO page_embeddings (page_id, field_name, embedding)
SELECT id, 'content', embedding
FROM pages
WHERE embedding IS NOT NULL;

-- Drop old column and index
DROP INDEX IF EXISTS idx_pages_embedding;
ALTER TABLE pages DROP COLUMN IF EXISTS embedding;

-- Drop old match_pages (different param list = separate overload in Postgres)
DROP FUNCTION IF EXISTS match_pages(
    extensions.vector, DOUBLE PRECISION, INTEGER, TEXT, UUID
);

-- Replace match_pages to query through page_embeddings
CREATE OR REPLACE FUNCTION match_pages(
    query_embedding extensions.vector(1024),
    match_threshold DOUBLE PRECISION DEFAULT 0.5,
    match_count INTEGER DEFAULT 10,
    filter_workspace TEXT DEFAULT NULL,
    filter_project_id UUID DEFAULT NULL,
    filter_field_name TEXT DEFAULT NULL
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
LANGUAGE sql STABLE
SET search_path = public, extensions
AS $$
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
    ORDER BY pe.embedding <=> query_embedding
    LIMIT match_count;
$$;
