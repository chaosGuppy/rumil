-- Exchange forks: admin-only side-effect-free re-runs of a captured LLM
-- exchange with edited overrides. Lives outside the staged-runs visibility
-- model entirely — pure operator state, never read by orchestrators or
-- context builders.
--
-- A fork carries a partial `overrides` JSON (only the fields that differ
-- from the base exchange). Multiple samples of the same overrides share an
-- `overrides_hash` and are sequenced by `sample_index` so the UI can stack
-- them as a single column.
--
-- `promoted_to_run_id` is reserved for the future "continue from here as a
-- staged run" expansion; null until used.

CREATE TABLE public.exchange_forks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    base_exchange_id UUID NOT NULL REFERENCES public.call_llm_exchanges(id) ON DELETE CASCADE,
    overrides JSONB NOT NULL DEFAULT '{}',
    overrides_hash TEXT NOT NULL,
    sample_index INT NOT NULL DEFAULT 0,
    model TEXT NOT NULL,
    temperature NUMERIC,
    response_text TEXT,
    tool_calls JSONB DEFAULT '[]',
    stop_reason TEXT,
    input_tokens INT,
    output_tokens INT,
    cache_creation_input_tokens INT,
    cache_read_input_tokens INT,
    duration_ms INT,
    cost_usd NUMERIC,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by TEXT,
    promoted_to_run_id TEXT REFERENCES public.runs(id)
);

CREATE INDEX idx_exchange_forks_base ON public.exchange_forks(base_exchange_id);
CREATE INDEX idx_exchange_forks_group
    ON public.exchange_forks(base_exchange_id, overrides_hash, sample_index);

ALTER TABLE public.exchange_forks ENABLE ROW LEVEL SECURITY;
