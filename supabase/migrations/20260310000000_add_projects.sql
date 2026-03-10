-- Create projects table
CREATE TABLE projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Seed default project
INSERT INTO projects (name) VALUES ('default');

-- Add project_id to pages and calls
ALTER TABLE pages ADD COLUMN project_id UUID REFERENCES projects(id);
ALTER TABLE calls ADD COLUMN project_id UUID REFERENCES projects(id);

-- Backfill existing rows to 'default' project
UPDATE pages SET project_id = (SELECT id FROM projects WHERE name = 'default');
UPDATE calls SET project_id = (SELECT id FROM projects WHERE name = 'default');

-- Make NOT NULL after backfill
ALTER TABLE pages ALTER COLUMN project_id SET NOT NULL;
ALTER TABLE calls ALTER COLUMN project_id SET NOT NULL;

-- Index
CREATE INDEX idx_pages_project ON pages(project_id);
CREATE INDEX idx_calls_project ON calls(project_id);

-- Update get_root_questions to filter by project_id
CREATE OR REPLACE FUNCTION get_root_questions(ws TEXT, pid UUID DEFAULT NULL)
RETURNS SETOF pages
LANGUAGE sql STABLE AS $$
    SELECT p.* FROM pages p
    WHERE p.page_type = 'question'
      AND p.workspace = ws
      AND p.is_superseded = FALSE
      AND (pid IS NULL OR p.project_id = pid)
      AND p.id NOT IN (
          SELECT to_page_id FROM page_links WHERE link_type = 'child_question'
      )
    ORDER BY p.created_at DESC;
$$;

-- Update get_ingest_history to filter by project_id
CREATE OR REPLACE FUNCTION get_ingest_history(pid UUID DEFAULT NULL)
RETURNS TABLE(source_id TEXT, question_id TEXT)
LANGUAGE sql STABLE AS $$
    SELECT DISTINCT c.scope_page_id AS source_id, pl.to_page_id AS question_id
    FROM calls c
    JOIN pages p ON p.provenance_call_id = c.id
    JOIN page_links pl ON pl.from_page_id = p.id AND pl.link_type = 'consideration'
    WHERE c.call_type = 'ingest' AND c.status = 'complete'
      AND (pid IS NULL OR c.project_id = pid);
$$;
