-- Store the full request body and full response body as sent/received,
-- so exchanges are byte-faithful enough to replay or re-render without
-- depending on the existing decomposed columns (system_prompt,
-- user_messages, response_text, tool_calls, thinking_blocks). The
-- decomposed columns become derived/cached views of these two; new
-- content-block types can be surfaced without a migration.
--
-- provider_request_id is the request id assigned by the LLM provider
-- (Anthropic returns this in response headers as ``request-id`` and
-- exposes it via ``stream.request_id`` / ``response._request_id``).
-- Stored top-level so support-ticket lookups don't have to parse JSONB.

ALTER TABLE public.call_llm_exchanges
    ADD COLUMN request JSONB,
    ADD COLUMN response JSONB,
    ADD COLUMN provider_request_id TEXT;
