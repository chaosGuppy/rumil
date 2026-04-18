-- Chat conversation persistence: store parma chat sessions so users can
-- resume prior conversations. Respects staged-run visibility rules.

CREATE TABLE chat_conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    question_id TEXT,
    title TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    staged BOOLEAN NOT NULL DEFAULT false,
    run_id UUID
);

CREATE INDEX idx_chat_conversations_project ON chat_conversations(project_id, updated_at DESC)
    WHERE deleted_at IS NULL;
CREATE INDEX idx_chat_conversations_run ON chat_conversations(run_id) WHERE staged = true;

ALTER TABLE chat_conversations ENABLE ROW LEVEL SECURITY;

CREATE TABLE chat_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool_use', 'tool_result', 'system')),
    content JSONB NOT NULL DEFAULT '{}',
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    seq INTEGER NOT NULL DEFAULT 0,
    staged BOOLEAN NOT NULL DEFAULT false,
    run_id UUID
);

CREATE INDEX idx_chat_messages_conversation ON chat_messages(conversation_id, seq ASC);
CREATE INDEX idx_chat_messages_run ON chat_messages(run_id) WHERE staged = true;

ALTER TABLE chat_messages ENABLE ROW LEVEL SECURITY;
