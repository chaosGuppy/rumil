CREATE TABLE public.call_llm_exchanges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id TEXT NOT NULL REFERENCES calls(id),
    run_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    round INT NOT NULL DEFAULT 0,
    system_prompt TEXT,
    user_message TEXT,
    response_text TEXT,
    tool_calls JSONB DEFAULT '[]',
    input_tokens INT,
    output_tokens INT,
    error TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_llm_exchanges_call ON public.call_llm_exchanges(call_id);
CREATE INDEX idx_llm_exchanges_run ON public.call_llm_exchanges(run_id);
