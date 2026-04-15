CREATE TABLE ab_eval_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id_a TEXT NOT NULL,
    run_id_b TEXT NOT NULL,
    question_id_a TEXT,
    question_id_b TEXT,
    overall_assessment TEXT NOT NULL DEFAULT '',
    dimension_reports JSONB NOT NULL DEFAULT '[]'::jsonb,
    project_id UUID REFERENCES projects(id),
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_ab_eval_reports_project ON ab_eval_reports(project_id);
