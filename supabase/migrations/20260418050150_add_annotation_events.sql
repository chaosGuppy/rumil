-- Broad-surface annotation substrate (MVP).
--
-- One append-only events table for human- and model-authored annotations on
-- pages, spans, calls, and specific trace events. Aggregation is a query-time
-- concern: rows are never collapsed or rewritten at write time.
--
-- Coexists with page_flags for now; no backfill or migration of existing
-- flag rows. See marketplace-thread/28-annotation-primitives.md.
--
-- Staging: staged + run_id work the same way they do on page_flags and
-- reputation_events. Non-staged readers see staged=false rows only; staged
-- runs see baseline plus their own run_id rows (see "Staged Runs and the
-- Mutation Log" in CLAUDE.md).

CREATE TABLE annotation_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID REFERENCES projects(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
    annotation_type TEXT NOT NULL,
    author_type TEXT NOT NULL,
    author_id TEXT NOT NULL,
    target_page_id TEXT REFERENCES pages(id) ON DELETE CASCADE,
    target_call_id TEXT REFERENCES calls(id) ON DELETE CASCADE,
    target_event_seq INTEGER,
    span_start INTEGER,
    span_end INTEGER,
    category TEXT,
    note TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL DEFAULT '{}',
    extra JSONB NOT NULL DEFAULT '{}',
    staged BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_annotation_events_target_page ON annotation_events (target_page_id);
CREATE INDEX idx_annotation_events_target_call ON annotation_events (target_call_id);
CREATE INDEX idx_annotation_events_run ON annotation_events (run_id);
CREATE INDEX idx_annotation_events_project_type ON annotation_events (project_id, annotation_type);
CREATE INDEX idx_annotation_events_author ON annotation_events (author_type, author_id);
CREATE INDEX idx_annotation_events_staged ON annotation_events (staged) WHERE staged = TRUE;

ALTER TABLE annotation_events ENABLE ROW LEVEL SECURITY;
