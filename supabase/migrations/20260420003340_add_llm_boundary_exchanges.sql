-- Append-only log of every Anthropic API exchange.
-- Captures the transport view (full request + response, regardless of caller)
-- alongside the existing call_llm_exchanges call-semantics view.
-- run_id / call_id are nullable so chat turns and ad-hoc utility calls
-- without those scopes still get logged.

CREATE TABLE public.llm_boundary_exchanges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID REFERENCES public.projects(id) ON DELETE CASCADE,
    run_id TEXT,
    call_id TEXT REFERENCES public.calls(id) ON DELETE SET NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    latency_ms INT,
    model TEXT NOT NULL,
    request_json JSONB NOT NULL,
    response_json JSONB,
    usage JSONB,
    stop_reason TEXT,
    error_class TEXT,
    error_message TEXT,
    http_status INT,
    source TEXT NOT NULL,
    streamed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX llm_boundary_exchanges_run_id_idx
    ON public.llm_boundary_exchanges(run_id)
    WHERE run_id IS NOT NULL;
CREATE INDEX llm_boundary_exchanges_call_id_idx
    ON public.llm_boundary_exchanges(call_id)
    WHERE call_id IS NOT NULL;
CREATE INDEX llm_boundary_exchanges_project_started_idx
    ON public.llm_boundary_exchanges(project_id, started_at DESC);

ALTER TABLE public.llm_boundary_exchanges ENABLE ROW LEVEL SECURITY;
