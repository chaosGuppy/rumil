-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;

-- Add embedding column to pages (1024 dimensions, Voyage AI default)
ALTER TABLE pages ADD COLUMN embedding extensions.vector(1024);

-- Index for similarity search
CREATE INDEX idx_pages_embedding ON pages
    USING ivfflat (embedding extensions.vector_cosine_ops)
    WITH (lists = 100);

-- RPC: find similar pages by embedding vector
CREATE OR REPLACE FUNCTION match_pages(
    query_embedding extensions.vector(1024),
    match_threshold DOUBLE PRECISION DEFAULT 0.5,
    match_count INTEGER DEFAULT 10,
    filter_workspace TEXT DEFAULT NULL,
    filter_project_id UUID DEFAULT NULL
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
        1 - (p.embedding <=> query_embedding) AS similarity
    FROM pages p
    WHERE p.embedding IS NOT NULL
      AND p.is_superseded = FALSE
      AND 1 - (p.embedding <=> query_embedding) > match_threshold
      AND (filter_workspace IS NULL OR p.workspace = filter_workspace)
      AND (filter_project_id IS NULL OR p.project_id = filter_project_id)
    ORDER BY p.embedding <=> query_embedding
    LIMIT match_count;
$$;
