-- Fix chat_conversations.run_id and chat_messages.run_id type drift.
--
-- Original migration (20260418032423_chat_conversations.sql) declared
-- these columns as UUID, but every other table in the schema uses TEXT
-- for run_id (runs.id, reputation_events.run_id, pages.run_id,
-- page_links.run_id, ...). DB.create_chat_conversation writes self.run_id
-- as-is, so any non-UUID run_id string would fail the insert. There was
-- also no FK to runs, leaving these rows unreachable by DB.stage_run.
--
-- This migration:
--   1. Converts both columns to TEXT.
--   2. Adds a nullable FK to runs(id) ON DELETE SET NULL so stage_run
--      (and future retroactive-staging cleanup) can traverse these rows.

ALTER TABLE chat_conversations
    ALTER COLUMN run_id TYPE TEXT USING run_id::TEXT;

ALTER TABLE chat_messages
    ALTER COLUMN run_id TYPE TEXT USING run_id::TEXT;

ALTER TABLE chat_conversations
    ADD CONSTRAINT chat_conversations_run_id_fkey
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE SET NULL;

ALTER TABLE chat_messages
    ADD CONSTRAINT chat_messages_run_id_fkey
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE SET NULL;
