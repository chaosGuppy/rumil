-- Allow chat_messages rows with role='dispatch_result', which are written
-- asynchronously by the non-blocking dispatch handler when a fire-and-forget
-- research call completes. These rows carry the completion payload
-- (run_id, status, summary, trace_url) and render as chips in the UI.

ALTER TABLE chat_messages DROP CONSTRAINT IF EXISTS chat_messages_role_check;
ALTER TABLE chat_messages ADD CONSTRAINT chat_messages_role_check
    CHECK (role IN ('user', 'assistant', 'tool_use', 'tool_result', 'system', 'dispatch_result'));
