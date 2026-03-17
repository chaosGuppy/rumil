-- Return pages that have no embedding row for the given field_name.
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
