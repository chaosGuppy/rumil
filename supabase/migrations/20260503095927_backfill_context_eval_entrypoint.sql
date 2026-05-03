-- Backfill `entrypoint = 'context_eval'` on historical context-builder eval
-- runs. The runs were created before scripts/run_context_eval.py started
-- tagging them, so the experiments listing query (which filters on
-- entrypoint) wouldn't surface them. Both arms (gold + candidate) carry a
-- config.eval.role tag, which only run_context_eval.py writes — so it's
-- a safe positive identifier. Idempotent: only touches NULL entrypoints.

UPDATE runs
SET entrypoint = 'context_eval'
WHERE entrypoint IS NULL
  AND config->'eval'->>'role' IN ('gold', 'candidate');
