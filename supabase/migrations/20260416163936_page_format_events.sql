CREATE TABLE page_format_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    page_id TEXT NOT NULL,
    detail TEXT NOT NULL,
    call_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    tags JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_page_format_events_run_id ON page_format_events(run_id);
CREATE INDEX idx_page_format_events_call_id ON page_format_events(call_id);
ALTER TABLE page_format_events ENABLE ROW LEVEL SECURITY;
