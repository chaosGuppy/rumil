-- Chat conversation persistence: stores chat sessions so users can
-- resume prior conversations. Respects staged-run visibility rules.
--
-- Columns collapse several brian/exp migrations:
--   * run_id is TEXT (matches runs.id) with FK to runs ON DELETE SET NULL.
--   * chat_messages.question_id lets a conversation span multiple questions
--     within a project without losing context.
--   * parent_conversation_id / branched_at_seq record "branch from message N"
--     so the UI can show provenance. ON DELETE SET NULL keeps branches alive
--     when a soft-deleted parent is hard-deleted.

CREATE TABLE IF NOT EXISTS chat_conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    question_id TEXT,
    title TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    staged BOOLEAN NOT NULL DEFAULT false,
    run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
    parent_conversation_id UUID REFERENCES chat_conversations(id) ON DELETE SET NULL,
    branched_at_seq INTEGER
);

CREATE INDEX IF NOT EXISTS idx_chat_conversations_project
    ON chat_conversations(project_id, updated_at DESC)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_chat_conversations_run
    ON chat_conversations(run_id) WHERE staged = true;
CREATE INDEX IF NOT EXISTS idx_chat_conversations_parent
    ON chat_conversations(parent_conversation_id)
    WHERE parent_conversation_id IS NOT NULL;

ALTER TABLE chat_conversations ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS chat_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool_use', 'tool_result', 'system', 'dispatch_result')),
    content JSONB NOT NULL DEFAULT '{}',
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    seq INTEGER NOT NULL DEFAULT 0,
    staged BOOLEAN NOT NULL DEFAULT false,
    run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
    question_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation ON chat_messages(conversation_id, seq ASC);
CREATE INDEX IF NOT EXISTS idx_chat_messages_run ON chat_messages(run_id) WHERE staged = true;
CREATE INDEX IF NOT EXISTS idx_chat_messages_question
    ON chat_messages(question_id) WHERE question_id IS NOT NULL;

ALTER TABLE chat_messages ENABLE ROW LEVEL SECURITY;
