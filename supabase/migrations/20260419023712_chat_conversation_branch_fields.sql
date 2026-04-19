-- Branching metadata for chat_conversations.
--
-- Users can "branch from this message" in the chat UI when a conversation
-- has gone off the rails at message N: we copy messages 0..N into a new
-- conversation and the user continues there. The parent conversation is
-- preserved unchanged. These two columns let the new (child) conversation
-- remember where it came from:
--
--   parent_conversation_id — the source conversation id (nullable; null
--     means "not branched, this is an original conversation").
--   branched_at_seq       — the seq in the parent that we branched from
--     (i.e. the highest seq copied into the new conversation). Nullable
--     for the same reason.
--
-- We use ON DELETE SET NULL so cascading-deleting a parent does NOT
-- cascade into its branches; they survive as orphans (still readable,
-- the badge just can't resolve a parent title). This matches our
-- soft-delete UX — the parent "going away" shouldn't take branches with it.

ALTER TABLE chat_conversations
    ADD COLUMN parent_conversation_id UUID,
    ADD COLUMN branched_at_seq INTEGER;

ALTER TABLE chat_conversations
    ADD CONSTRAINT chat_conversations_parent_fkey
    FOREIGN KEY (parent_conversation_id)
    REFERENCES chat_conversations(id)
    ON DELETE SET NULL;

CREATE INDEX idx_chat_conversations_parent
    ON chat_conversations(parent_conversation_id)
    WHERE parent_conversation_id IS NOT NULL;
