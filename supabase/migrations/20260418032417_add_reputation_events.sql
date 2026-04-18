-- Multi-source reputation substrate.
--
-- Append-only, per-source raw scores. Aggregation is a query-time concern:
-- the table MUST NOT collapse sources or dimensions at write time. See
-- marketplace-thread/13-reputation-governance.md for the "don't collapse
-- eval-vs-human" invariant.
--
-- run_id / source_call_id are TEXT to match the existing runs and calls
-- tables (runs.id, calls.id are TEXT; only projects.id is UUID). Respect
-- staging: the staged flag + run_id are flipped by stage_run() /
-- commit_staged_run() on rows whose run_id matches, just like pages and
-- page_links.

CREATE TABLE reputation_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    dimension TEXT NOT NULL,
    score DOUBLE PRECISION NOT NULL,
    orchestrator TEXT,
    task_shape JSONB,
    source_call_id TEXT,
    extra JSONB NOT NULL DEFAULT '{}',
    staged BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_reputation_events_run ON reputation_events (run_id);
CREATE INDEX idx_reputation_events_project_source ON reputation_events (project_id, source);
CREATE INDEX idx_reputation_events_dimension ON reputation_events (dimension);
CREATE INDEX idx_reputation_events_staged ON reputation_events (staged) WHERE staged = TRUE;

ALTER TABLE reputation_events ENABLE ROW LEVEL SECURITY;
