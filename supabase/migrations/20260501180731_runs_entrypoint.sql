-- Tag the entry point that created a run (e.g. 'run_call') so the experiments
-- feed can filter cleanly without heuristics. NULL for legacy / un-tagged rows.

ALTER TABLE runs ADD COLUMN entrypoint TEXT;

CREATE INDEX runs_entrypoint_idx ON runs (entrypoint) WHERE entrypoint IS NOT NULL;
