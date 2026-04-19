-- Alert configs: user/run/project scoped rules that produce "fired alert"
-- summaries when an orchestrator tick or reader asks for them.
--
-- Kinds supported in v1:
--   cost_threshold    — params.pct (default 0.8) of runs.budget / actual USD
--   stall_timeout     — params.minutes (default 15) without a new call completion
--   confusion_spike   — params.window_min (default 30) + params.threshold (default 2.0)
--
-- Evaluation is compute-on-read in src/rumil/alerts/evaluator.py. No
-- scheduler yet — callers (orchestrator checkpoints, GET /runs/{id}/alerts)
-- invoke the evaluator when they want fresh results. Persistence of fired
-- events is deferred (no alert_events table) — callers either display or
-- act on the returned list.
--
-- Scope resolution (match from most to least specific):
--   1. rows where run_id = this run → override project-wide config
--   2. rows where project_id = this project → override global defaults
--   3. built-in defaults (no DB row needed)
--
-- ``enabled=false`` disables a rule without deleting the row; useful for
-- per-run mute without losing params.

CREATE TABLE alert_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID REFERENCES projects(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL
        CHECK (kind IN ('cost_threshold', 'stall_timeout', 'confusion_spike')),
    params JSONB NOT NULL DEFAULT '{}',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_alert_configs_run ON alert_configs(run_id) WHERE run_id IS NOT NULL;
CREATE INDEX idx_alert_configs_project ON alert_configs(project_id) WHERE project_id IS NOT NULL;

ALTER TABLE alert_configs ENABLE ROW LEVEL SECURITY;
