"""ChatStore: suggestions + chat conversations + chat messages + branching.

Suggestions and chat live together because they're both user-facing
asynchronous artifacts — suggestions are system-generated proposals
surfaced in the chat UI; chat messages are the other half of the same
interaction model. All three tables carry ``staged`` / ``run_id`` so
staged runs are isolated from baseline readers (see "Staged Runs and
the Mutation Log" in CLAUDE.md).

Branching is non-destructive: ``branch_chat_conversation`` clones
messages 0..at_seq into a new conversation with fresh primary keys,
leaving the source untouched.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from rumil.db.row_helpers import (
    _row_to_chat_conversation,
    _row_to_chat_message,
    _row_to_suggestion,
    _rows,
)
from rumil.models import (
    ChatConversation,
    ChatMessage,
    ChatMessageRole,
    Suggestion,
    SuggestionStatus,
)

if TYPE_CHECKING:
    from rumil.database import DB


class ChatStore:
    """Suggestions + conversations + messages + branching."""

    def __init__(self, db: "DB") -> None:
        self._db = db

    @property
    def client(self) -> Any:
        return self._db.client

    async def save_suggestion(self, suggestion: Suggestion) -> None:
        """Save a suggestion to the database."""
        await self._db._execute(
            self.client.table("suggestions").upsert(
                {
                    "id": suggestion.id,
                    "project_id": suggestion.project_id or self._db.project_id,
                    "workspace": suggestion.workspace,
                    "run_id": suggestion.run_id or self._db.run_id,
                    "suggestion_type": suggestion.suggestion_type.value,
                    "target_page_id": suggestion.target_page_id,
                    "source_page_id": suggestion.source_page_id,
                    "payload": suggestion.payload,
                    "status": suggestion.status.value,
                    "created_at": suggestion.created_at.isoformat(),
                    "reviewed_at": (
                        suggestion.reviewed_at.isoformat() if suggestion.reviewed_at else None
                    ),
                    "staged": suggestion.staged,
                }
            )
        )

    async def get_pending_suggestions(
        self,
        target_page_id: str | None = None,
    ) -> list[Suggestion]:
        """Get pending suggestions, optionally filtered by target page."""
        query = (
            self.client.table("suggestions")
            .select("*")
            .eq("project_id", self._db.project_id)
            .eq("status", "pending")
        )
        if target_page_id:
            query = query.eq("target_page_id", target_page_id)
        query = self._db._staged_filter(query)
        query = query.order("created_at", desc=True)
        rows = _rows(await self._db._execute(query))
        return [_row_to_suggestion(r) for r in rows]

    async def get_suggestions(
        self,
        status: str = "pending",
        target_page_id: str | None = None,
    ) -> list[Suggestion]:
        """Get suggestions filtered by status, optionally by target page."""
        query = (
            self.client.table("suggestions")
            .select("*")
            .eq("project_id", self._db.project_id)
            .eq("status", status)
        )
        if target_page_id:
            query = query.eq("target_page_id", target_page_id)
        query = self._db._staged_filter(query)
        query = query.order("created_at", desc=True)
        rows = _rows(await self._db._execute(query))
        return [_row_to_suggestion(r) for r in rows]

    async def get_suggestion(self, suggestion_id: str) -> Suggestion | None:
        """Fetch a single suggestion by ID."""
        rows = _rows(
            await self._db._execute(
                self.client.table("suggestions").select("*").eq("id", suggestion_id)
            )
        )
        return _row_to_suggestion(rows[0]) if rows else None

    async def update_suggestion_status(
        self,
        suggestion_id: str,
        status: SuggestionStatus,
    ) -> None:
        """Update a suggestion's status (accept/reject/dismiss)."""
        update: dict[str, Any] = {"status": status.value}
        if status != SuggestionStatus.PENDING:
            update["reviewed_at"] = datetime.now(UTC).isoformat()
        await self._db._execute(
            self.client.table("suggestions").update(update).eq("id", suggestion_id)
        )

    async def create_chat_conversation(
        self,
        project_id: str,
        question_id: str | None = None,
        title: str = "",
    ) -> ChatConversation:
        """Create a new chat conversation row."""
        conv = ChatConversation(
            project_id=project_id,
            question_id=question_id,
            title=title,
            staged=self._db.staged,
            run_id=self._db.run_id if self._db.staged else None,
        )
        await self._db._execute(
            self.client.table("chat_conversations").insert(
                {
                    "id": conv.id,
                    "project_id": conv.project_id,
                    "question_id": conv.question_id,
                    "title": conv.title,
                    "created_at": conv.created_at.isoformat(),
                    "updated_at": conv.updated_at.isoformat(),
                    "staged": conv.staged,
                    "run_id": conv.run_id,
                }
            )
        )
        return conv

    async def get_chat_conversation(self, conversation_id: str) -> ChatConversation | None:
        """Fetch a single conversation (staged-run-aware, excludes soft-deleted)."""
        query = (
            self.client.table("chat_conversations")
            .select("*")
            .eq("id", conversation_id)
            .is_("deleted_at", "null")
        )
        query = self._db._staged_filter(query)
        rows = _rows(await self._db._execute(query))
        return _row_to_chat_conversation(rows[0]) if rows else None

    async def list_chat_conversations(
        self,
        project_id: str,
        limit: int = 50,
        offset: int = 0,
        question_id: str | None = None,
    ) -> Sequence[ChatConversation]:
        """List conversations for a project, most-recently-updated first."""
        query = (
            self.client.table("chat_conversations")
            .select("*")
            .eq("project_id", project_id)
            .is_("deleted_at", "null")
        )
        if question_id:
            query = query.eq("question_id", question_id)
        query = self._db._staged_filter(query).order("updated_at", desc=True)
        query = query.range(offset, offset + max(0, limit - 1))
        rows = _rows(await self._db._execute(query))
        return [_row_to_chat_conversation(r) for r in rows]

    async def update_chat_conversation(
        self,
        conversation_id: str,
        title: str | None = None,
        touch: bool = False,
    ) -> None:
        """Rename or touch updated_at on a conversation."""
        update: dict[str, Any] = {}
        if title is not None:
            update["title"] = title
        if touch or title is not None:
            update["updated_at"] = datetime.now(UTC).isoformat()
        if not update:
            return
        await self._db._execute(
            self.client.table("chat_conversations").update(update).eq("id", conversation_id)
        )

    async def soft_delete_chat_conversation(self, conversation_id: str) -> None:
        """Mark a conversation as soft-deleted."""
        await self._db._execute(
            self.client.table("chat_conversations")
            .update({"deleted_at": datetime.now(UTC).isoformat()})
            .eq("id", conversation_id)
        )

    async def save_chat_message(
        self,
        conversation_id: str,
        role: ChatMessageRole,
        content: dict,
        seq: int | None = None,
        question_id: str | None = None,
    ) -> ChatMessage:
        """Append a message to a conversation. Auto-assigns seq if omitted."""
        if seq is None:
            seq = await self._next_chat_message_seq(conversation_id)
        msg = ChatMessage(
            conversation_id=conversation_id,
            role=role,
            content=content,
            seq=seq,
            staged=self._db.staged,
            run_id=self._db.run_id if self._db.staged else None,
            question_id=question_id,
        )
        await self._db._execute(
            self.client.table("chat_messages").insert(
                {
                    "id": msg.id,
                    "conversation_id": msg.conversation_id,
                    "role": msg.role.value,
                    "content": msg.content,
                    "seq": msg.seq,
                    "ts": msg.ts.isoformat(),
                    "staged": msg.staged,
                    "run_id": msg.run_id,
                    "question_id": msg.question_id,
                }
            )
        )
        return msg

    async def _next_chat_message_seq(self, conversation_id: str) -> int:
        """Return the next sequence number for a conversation."""
        rows = _rows(
            await self._db._execute(
                self.client.table("chat_messages")
                .select("seq")
                .eq("conversation_id", conversation_id)
                .order("seq", desc=True)
                .limit(1)
            )
        )
        return (rows[0]["seq"] + 1) if rows else 0

    async def list_chat_messages(
        self,
        conversation_id: str,
    ) -> Sequence[ChatMessage]:
        """List all messages in a conversation in order."""
        query = (
            self.client.table("chat_messages").select("*").eq("conversation_id", conversation_id)
        )
        query = self._db._staged_filter(query).order("seq", desc=False)
        rows = _rows(await self._db._execute(query))
        return [_row_to_chat_message(r) for r in rows]

    async def branch_chat_conversation(
        self,
        source_conversation_id: str,
        at_seq: int,
        title: str | None = None,
    ) -> ChatConversation:
        """Branch a conversation at `at_seq`, copying messages 0..at_seq into a new convo.

        The source conversation is left untouched — branching is non-destructive,
        the whole point is to preserve the original thread while forking a new
        one from a chosen point. Returns the newly-created conversation.
        """
        source = await self.get_chat_conversation(source_conversation_id)
        if source is None:
            raise ValueError(f"source conversation {source_conversation_id} not found")
        if at_seq < 0:
            raise ValueError(f"at_seq must be >= 0, got {at_seq}")

        messages = await self.list_chat_messages(source_conversation_id)
        if not messages:
            raise ValueError(
                f"at_seq={at_seq} does not correspond to any message in "
                f"conversation {source_conversation_id}"
            )
        max_seq = messages[-1].seq
        if at_seq > max_seq:
            raise ValueError(
                f"at_seq={at_seq} does not correspond to any message in "
                f"conversation {source_conversation_id} (max seq is {max_seq})"
            )
        to_copy = [m for m in messages if m.seq <= at_seq]
        if not to_copy:
            raise ValueError(
                f"at_seq={at_seq} does not correspond to any message in "
                f"conversation {source_conversation_id}"
            )

        effective_seq = to_copy[-1].seq
        derived_title = title or f"branch of {source.title or '(untitled)'} @ msg {effective_seq}"

        new_conv = ChatConversation(
            project_id=source.project_id,
            question_id=source.question_id,
            title=derived_title,
            staged=self._db.staged,
            run_id=self._db.run_id if self._db.staged else None,
            parent_conversation_id=source.id,
            branched_at_seq=effective_seq,
        )
        await self._db._execute(
            self.client.table("chat_conversations").insert(
                {
                    "id": new_conv.id,
                    "project_id": new_conv.project_id,
                    "question_id": new_conv.question_id,
                    "title": new_conv.title,
                    "created_at": new_conv.created_at.isoformat(),
                    "updated_at": new_conv.updated_at.isoformat(),
                    "staged": new_conv.staged,
                    "run_id": new_conv.run_id,
                    "parent_conversation_id": new_conv.parent_conversation_id,
                    "branched_at_seq": new_conv.branched_at_seq,
                }
            )
        )

        now_iso = datetime.now(UTC).isoformat()
        new_rows = [
            {
                "id": str(uuid.uuid4()),
                "conversation_id": new_conv.id,
                "role": m.role.value,
                "content": m.content,
                "seq": m.seq,
                "ts": now_iso,
                "staged": new_conv.staged,
                "run_id": new_conv.run_id,
                "question_id": m.question_id,
            }
            for m in to_copy
        ]
        if new_rows:
            await self._db._execute(self.client.table("chat_messages").insert(new_rows))

        return new_conv
