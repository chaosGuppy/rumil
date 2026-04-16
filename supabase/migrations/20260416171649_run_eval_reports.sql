CREATE TABLE run_eval_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id TEXT NOT NULL,
    question_id TEXT,
    overall_assessment TEXT NOT NULL DEFAULT '',
    dimension_reports JSONB NOT NULL DEFAULT '[]'::jsonb,
    project_id UUID REFERENCES projects(id),
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_run_eval_reports_run ON run_eval_reports(run_id);
CREATE INDEX idx_run_eval_reports_project ON run_eval_reports(project_id);

ALTER TABLE run_eval_reports ENABLE ROW LEVEL SECURITY;
