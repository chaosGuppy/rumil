CREATE TABLE suggestions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id text NOT NULL DEFAULT '',
    workspace text NOT NULL DEFAULT 'research',
    run_id text NOT NULL DEFAULT '',
    suggestion_type text NOT NULL CHECK (suggestion_type IN (
        'cascade_review', 'relevel', 'resolve_tension', 'merge_duplicate', 'auto_investigate'
    )),
    target_page_id text NOT NULL REFERENCES pages(id),
    source_page_id text,
    payload jsonb NOT NULL DEFAULT '{}',
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected', 'dismissed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    reviewed_at timestamptz,
    staged boolean NOT NULL DEFAULT false
);

CREATE INDEX idx_suggestions_status ON suggestions(project_id, status) WHERE status = 'pending';
CREATE INDEX idx_suggestions_target ON suggestions(target_page_id);
