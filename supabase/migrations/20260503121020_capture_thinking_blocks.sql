-- Capture summarized chain-of-thought (and redacted thinking blocks) from
-- Anthropic responses on the per-exchange row. Shape:
--   { "thinking":          [{"content": "...", "signature": "..."}, ...],
--     "redacted_thinking": [{"data": "..."}, ...] }
-- NULL when the model returned no thinking blocks (e.g. Haiku, or
-- non-thinking models). No index — we don't query by it.
ALTER TABLE call_llm_exchanges
  ADD COLUMN thinking_blocks jsonb;
