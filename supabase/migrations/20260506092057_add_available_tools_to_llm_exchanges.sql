-- Captures the list of tool definitions made available to the model on a
-- given exchange. Stored as JSONB (one element per tool, in Anthropic
-- request shape: {name, description, input_schema}). NULL when the call
-- ran without any tools.
ALTER TABLE public.call_llm_exchanges ADD COLUMN available_tools JSONB;
