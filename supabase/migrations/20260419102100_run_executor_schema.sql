-- Run executor schema: lifecycle state on `runs`, per-call cost accounting
-- in `call_costs`, and stage-boundary checkpoints in `run_checkpoints`.
--
-- This is Phase 1 of promoting Run to a real control plane. The matching
-- src/rumil/run_executor/ package that consumes these columns is not yet
-- landed; the schema is additive and safe on its own. New rows get the
-- defaults; existing rows are backfilled at the end of this migration.

-- runs lifecycle
ALTER TABLE public.runs
    ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','paused','complete','failed','cancelled')),
    ADD COLUMN started_at TIMESTAMPTZ,
    ADD COLUMN finished_at TIMESTAMPTZ,
    ADD COLUMN cost_usd_cents BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN paused_at TIMESTAMPTZ,
    ADD COLUMN cancel_reason TEXT;

CREATE INDEX idx_runs_status ON public.runs(status) WHERE status IN ('running','paused');

-- Per-call cost accounting. The `budget` table keeps counts-of-calls
-- semantics (for pacing); this one is the source of truth for dollars
-- and is what future cost dashboards / BudgetGate.commit aggregate.
CREATE TABLE public.call_costs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    call_type TEXT NOT NULL,
    prompt_version TEXT,
    model TEXT,
    input_tokens INTEGER,
    cache_read_tokens INTEGER,
    output_tokens INTEGER,
    usd NUMERIC(12, 6) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_call_costs_run ON public.call_costs(run_id);
CREATE INDEX idx_call_costs_call_type_time ON public.call_costs(call_type, created_at DESC);
CREATE INDEX idx_call_costs_prompt_version_time
    ON public.call_costs(prompt_version, created_at DESC)
    WHERE prompt_version IS NOT NULL;

ALTER TABLE public.call_costs ENABLE ROW LEVEL SECURITY;

-- Stage-boundary checkpoints for resumable runs. Kinds:
--   'orchestrator_tick' — between dispatch batches; payload is the pending
--                         sequences + orchestrator-local state.
--   'call_stage'        — mirrors CallStage transitions; payload is the
--                         minimal info to resume the call mid-stage.
--   'cost_committed'    — a BudgetGate.commit that landed; payload is the
--                         reservation metadata + actual USD.
-- seq is monotonically increasing per run_id, never reused.
CREATE TABLE public.run_checkpoints (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, seq)
);

ALTER TABLE public.run_checkpoints ENABLE ROW LEVEL SECURITY;

-- Backfill runs.status from existing call presence.
-- Any run with a live RUNNING call → 'running'. Any run whose calls are
-- all non-PENDING and at least one is COMPLETE → 'complete'. Everything
-- else → 'pending' (the default we just set).
UPDATE public.runs r
SET status = 'running'
WHERE status = 'pending'
  AND EXISTS (
      SELECT 1 FROM public.calls c
      WHERE c.run_id = r.id AND c.status = 'running'
  );

UPDATE public.runs r
SET status = 'complete'
WHERE status = 'pending'
  AND EXISTS (
      SELECT 1 FROM public.calls c WHERE c.run_id = r.id AND c.status = 'complete'
  )
  AND NOT EXISTS (
      SELECT 1 FROM public.calls c WHERE c.run_id = r.id AND c.status = 'running'
  );

-- Set started_at / finished_at from min / max created_at on the run's calls.
UPDATE public.runs r
SET started_at = sub.min_created, finished_at = sub.max_completed
FROM (
    SELECT run_id,
           MIN(created_at) AS min_created,
           MAX(completed_at) AS max_completed
    FROM public.calls
    WHERE run_id IS NOT NULL
    GROUP BY run_id
) sub
WHERE r.id = sub.run_id;
