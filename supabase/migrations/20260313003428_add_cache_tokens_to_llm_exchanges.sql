ALTER TABLE public.call_llm_exchanges
  ADD COLUMN cache_creation_input_tokens INT,
  ADD COLUMN cache_read_input_tokens INT;
