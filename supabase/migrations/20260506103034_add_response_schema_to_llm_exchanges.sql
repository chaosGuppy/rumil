-- Captures the JSON Schema the model was constrained to produce on a
-- structured call. Stored as JSONB in our internal shape:
-- {"name": <pydantic class name>, "schema": <JSON Schema dict>}.
-- NULL for unstructured calls.
ALTER TABLE public.call_llm_exchanges ADD COLUMN response_schema JSONB;
