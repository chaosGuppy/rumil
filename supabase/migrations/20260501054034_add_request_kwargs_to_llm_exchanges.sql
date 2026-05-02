-- Captures the effective non-bulky request-condition fields (thinking,
-- output_config, temperature, max_tokens) at exchange-write time so
-- forks (and any future audit) can reproduce the original condition
-- regardless of whether rumil.llm.thinking_config / effort_level rules
-- have changed since.
ALTER TABLE public.call_llm_exchanges ADD COLUMN request_kwargs JSONB;
