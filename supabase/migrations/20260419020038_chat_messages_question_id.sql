-- Record which question each chat message was asked against, so a
-- conversation can span multiple questions within a project without
-- losing context. The conversation's own `question_id` remains as a
-- "primary" pointer (typically the question where the chat began);
-- per-message question_id is what the UI uses to annotate turns that
-- occurred under a different question than the one currently in view.

ALTER TABLE chat_messages
    ADD COLUMN question_id TEXT;

CREATE INDEX idx_chat_messages_question ON chat_messages(question_id)
    WHERE question_id IS NOT NULL;
