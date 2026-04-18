"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  streamChatMessage,
  listChatConversations,
  getChatConversation,
  renameChatConversation,
  deleteChatConversation,
} from "@/lib/api";
import type { ChatToolUse, ChatConversationSummary } from "@/lib/api";
import { SlashCommandDropdown, useSlashCommands } from "./SlashCommands";

type MessageBlock =
  | { type: "text"; content: string }
  | { type: "tool"; tool: ChatToolUse };

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
  loading?: boolean;
  blocks?: MessageBlock[];
}

interface ChatPanelProps {
  questionId: string;
  questionHeadline: string;
  isOpen: boolean;
  onToggle: () => void;
  onMessageSent?: () => void;
  onNodeRef?: (nodeId: string) => void;
  onShowReview?: () => void;
  workspace?: string;
  projectId?: string;
}

function contentToText(content: unknown): string {
  if (typeof content === "string") return content;
  if (content && typeof content === "object" && "text" in content) {
    return String((content as { text: string }).text);
  }
  return "";
}

function persistedMessagesToUi(
  raw: Array<{ id: string; role: string; content: Record<string, unknown>; seq: number; ts: string }>,
): Message[] {
  const out: Message[] = [];
  for (const m of raw) {
    if (m.role === "user") {
      out.push({
        id: m.id,
        role: "user",
        content: contentToText(m.content),
        timestamp: new Date(m.ts),
      });
    } else if (m.role === "assistant") {
      const blocksIn = (m.content?.blocks ?? []) as Array<{ type: string; text?: string; name?: string; input?: Record<string, unknown> }>;
      const blocks: MessageBlock[] = [];
      let textAccum = "";
      for (const b of blocksIn) {
        if (b.type === "text") {
          textAccum += b.text ?? "";
          blocks.push({ type: "text", content: b.text ?? "" });
        } else if (b.type === "tool_use") {
          blocks.push({
            type: "tool",
            tool: { name: b.name ?? "", input: b.input ?? {}, result: "" },
          });
        }
      }
      out.push({
        id: m.id,
        role: "assistant",
        content: textAccum,
        timestamp: new Date(m.ts),
        blocks,
      });
    }
  }
  return out;
}

function formatTime(date: Date): string {
  return date.toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  });
}

const NODE_ID_RE = /\b([0-9a-f]{8})\b/g;

function processChildren(
  children: React.ReactNode,
  onNodeRef?: (id: string) => void,
): React.ReactNode {
  if (!onNodeRef) return children;
  if (!Array.isArray(children)) {
    if (typeof children === "string") {
      return <TextWithNodeRefs text={children} onNodeRef={onNodeRef} />;
    }
    return children;
  }
  return children.map((child, i) => {
    if (typeof child === "string") {
      return <TextWithNodeRefs key={i} text={child} onNodeRef={onNodeRef} />;
    }
    return child;
  });
}

function TextWithNodeRefs({
  text,
  onNodeRef,
}: {
  text: string;
  onNodeRef?: (id: string) => void;
}) {
  if (!onNodeRef) return <>{text}</>;
  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  const re = new RegExp(NODE_ID_RE);
  while ((match = re.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    const id = match[1];
    parts.push(
      <button
        key={match.index}
        onClick={() => onNodeRef(id)}
        className="node-ref-link"
      >
        {id}
      </button>,
    );
    lastIndex = re.lastIndex;
  }
  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }
  return <>{parts}</>;
}

function ToolBlock({ tu }: { tu: ChatToolUse }) {
  return (
    <div style={{ padding: "2px 0" }}>
      {tu.result ? "\u2713" : "\u27F3"} {tu.name}
      {tu.result ? ` \u2014 ${tu.result.slice(0, 80)}` : " \u2026"}
    </div>
  );
}

function TextContent({ text, onNodeRef }: { text: string; onNodeRef?: (id: string) => void }) {
  return (
    <div className="chat-markdown" style={{
      fontSize: "14px", lineHeight: 1.6, fontFamily: "var(--font-body-stack)",
    }}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children }) => <p>{processChildren(children, onNodeRef)}</p>,
          li: ({ children }) => <li>{processChildren(children, onNodeRef)}</li>,
          strong: ({ children }) => <strong>{processChildren(children, onNodeRef)}</strong>,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

function MessageEntry({
  message,
  onNodeRef,
}: {
  message: Message;
  onNodeRef?: (id: string) => void;
}) {
  const isUser = message.role === "user";
  const blocks = message.blocks;

  return (
    <div style={{ padding: "12px 0", borderBottom: "1px solid var(--border)" }}>
      <div style={{
        display: "flex", alignItems: "baseline", gap: "8px", marginBottom: "4px",
      }}>
        <span style={{
          fontFamily: "var(--font-mono-stack)", fontSize: "10px",
          letterSpacing: "0.06em", textTransform: "uppercase",
          color: isUser ? "var(--accent)" : "var(--node-claim)", fontWeight: 500,
        }}>
          {isUser ? "You" : "Rumil"}
        </span>
        <span style={{
          fontFamily: "var(--font-mono-stack)", fontSize: "9px",
          color: "var(--fg-dim)", letterSpacing: "0.02em",
        }}>
          {formatTime(message.timestamp)}
        </span>
      </div>

      {isUser ? (
        <div style={{ fontSize: "14px", lineHeight: 1.6, fontFamily: "var(--font-body-stack)" }}>
          {message.content.split("\n").map((line, i) => (
            <p key={i} style={{ margin: i === 0 ? "0" : "6px 0 0 0" }}>{line}</p>
          ))}
        </div>
      ) : blocks && blocks.length > 0 ? (
        <div style={{ borderLeft: "2px solid var(--border)", paddingLeft: "10px" }}>
          {blocks.map((block, i) =>
            block.type === "text" ? (
              block.content.trim() ? <TextContent key={i} text={block.content} onNodeRef={onNodeRef} /> : null
            ) : (
              <div key={i} style={{
                fontFamily: "var(--font-mono-stack)", fontSize: "10px",
                color: "var(--fg-dim)", letterSpacing: "0.02em", margin: "6px 0",
              }}>
                <ToolBlock tu={block.tool} />
              </div>
            ),
          )}
        </div>
      ) : message.content ? (
        <div style={{ borderLeft: "2px solid var(--border)", paddingLeft: "10px" }}>
          <TextContent text={message.content} onNodeRef={onNodeRef} />
        </div>
      ) : null}

      {message.loading && (
        <div className="thinking-indicator" style={{ marginTop: "4px" }}>
          <span className="thinking-dot" />
          <span className="thinking-text">thinking</span>
        </div>
      )}
    </div>
  );
}

export function ChatPanel({
  questionId,
  questionHeadline,
  isOpen,
  onToggle,
  onMessageSent,
  onNodeRef,
  onShowReview,
  workspace = "default",
  projectId,
}: ChatPanelProps) {
  const initialAssistantMessage: Message = {
    id: "initial",
    role: "assistant",
    content:
      "Ask me about this view \u2014 I can explain the reasoning behind claims, surface tensions between findings, or discuss what the research might be missing.",
    timestamp: new Date(),
  };
  const [messages, setMessages] = useState<Message[]>([initialAssistantMessage]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<ChatConversationSummary[]>([]);
  const [showSidebar, setShowSidebar] = useState(false);
  const [input, setInput] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const refreshConversations = useCallback(async () => {
    if (!projectId) return;
    try {
      const items = await listChatConversations(projectId, questionId || undefined);
      setConversations(items);
    } catch {
      /* ignore — API may not be available yet */
    }
  }, [projectId, questionId]);

  useEffect(() => {
    if (isOpen) refreshConversations();
  }, [isOpen, refreshConversations]);

  const handleNewChat = useCallback(() => {
    setConversationId(null);
    setMessages([initialAssistantMessage]);
    setShowSidebar(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleLoadConversation = useCallback(async (id: string) => {
    try {
      const detail = await getChatConversation(id);
      const uiMessages = persistedMessagesToUi(detail.messages);
      setMessages(uiMessages.length ? uiMessages : [initialAssistantMessage]);
      setConversationId(id);
      setShowSidebar(false);
    } catch (e) {
      console.error("Failed to load conversation", e);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleRenameConversation = useCallback(async (id: string, currentTitle: string) => {
    const next = typeof window !== "undefined" ? window.prompt("Rename conversation", currentTitle) : null;
    if (!next || next === currentTitle) return;
    try {
      await renameChatConversation(id, next);
      await refreshConversations();
    } catch (e) {
      console.error("Failed to rename", e);
    }
  }, [refreshConversations]);

  const handleDeleteConversation = useCallback(async (id: string) => {
    if (typeof window !== "undefined" && !window.confirm("Delete this conversation?")) return;
    try {
      await deleteChatConversation(id);
      if (id === conversationId) handleNewChat();
      await refreshConversations();
    } catch (e) {
      console.error("Failed to delete", e);
    }
  }, [conversationId, handleNewChat, refreshConversations]);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  useEffect(() => {
    if (isOpen && textareaRef.current) {
      textareaRef.current.focus();
    }
  }, [isOpen]);

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === "/") {
        e.preventDefault();
        onToggle();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onToggle]);

  const [isLoading, setIsLoading] = useState(false);
  const [model, setModel] = useState<"sonnet" | "opus" | "haiku">("sonnet");
  const { showDropdown, handleSelect: handleSlashSelect, handleDismiss } =
    useSlashCommands(input, setInput, textareaRef);

  const handleSubmit = useCallback(async () => {
    const trimmed = input.trim();
    if (!trimmed || isLoading) return;

    const modelCommands: Record<string, "sonnet" | "opus" | "haiku"> = {
      "/sonnet": "sonnet",
      "/opus": "opus",
      "/haiku": "haiku",
    };
    if (modelCommands[trimmed]) {
      setModel(modelCommands[trimmed]);
      setInput("");
      setMessages((prev) => [
        ...prev,
        {
          id: `sys-${Date.now()}`,
          role: "assistant" as const,
          content: `Switched to **${modelCommands[trimmed]}**.`,
          timestamp: new Date(),
        },
      ]);
      return;
    }

    if (trimmed === "/review") {
      setInput("");
      onShowReview?.();
      return;
    }

    const userMsg: Message = {
      id: `user-${Date.now()}`,
      role: "user",
      content: trimmed,
      timestamp: new Date(),
    };

    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setIsLoading(true);

    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }

    const assistantId = `asst-${Date.now()}`;
    setMessages((prev) => [
      ...prev,
      {
        id: assistantId,
        role: "assistant",
        content: "",
        timestamp: new Date(),
        loading: true,
        blocks: [],
      },
    ]);

    try {
      const apiMessages = [...messages, userMsg]
        .filter((m) => m.id !== "initial")
        .map((m) => ({ role: m.role, content: m.content }));

      let currentBlocks: MessageBlock[] = [];
      let currentText = "";

      const updateMsg = () => {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? { ...m, content: currentText, blocks: [...currentBlocks] }
              : m,
          ),
        );
      };

      await streamChatMessage(questionId, apiMessages, (event) => {
        if (event.type === "conversation") {
          const cid = (event.data.conversation_id as string) || null;
          if (cid && cid !== conversationId) setConversationId(cid);
          return;
        }
        if (event.type === "text") {
          const chunk = event.data.content as string;
          currentText += chunk;
          const lastIdx = currentBlocks.length - 1;
          if (lastIdx >= 0 && currentBlocks[lastIdx].type === "text") {
            currentBlocks[lastIdx] = { type: "text", content: currentText };
          } else {
            currentBlocks.push({ type: "text", content: currentText });
          }
          updateMsg();
        } else if (event.type === "tool_use_start") {
          currentText = "";
          currentBlocks.push({
            type: "tool",
            tool: { name: event.data.name as string, input: {}, result: "" },
          });
          updateMsg();
        } else if (event.type === "tool_use_result") {
          currentText = "";
          const name = event.data.name as string;
          const input = (event.data.input as Record<string, unknown>) || {};
          const result = event.data.result as string;
          let matched = false;
          currentBlocks = currentBlocks.map((b) => {
            if (!matched && b.type === "tool" && b.tool.name === name && !b.tool.result) {
              matched = true;
              return { type: "tool" as const, tool: { name, input, result } };
            }
            return b;
          });
          updateMsg();
          if (name === "create_question" || name === "dispatch_call") {
            onMessageSent?.();
          }
        } else if (event.type === "orchestrator_progress") {
          const msg = event.data.message as string;
          const lastIdx = currentBlocks.length - 1;
          if (lastIdx >= 0 && currentBlocks[lastIdx].type === "tool" && !currentBlocks[lastIdx].tool.result) {
            currentBlocks[lastIdx] = {
              type: "tool" as const,
              tool: { ...currentBlocks[lastIdx].tool, input: { ...currentBlocks[lastIdx].tool.input, _progress: msg } },
            };
          }
          updateMsg();
        } else if (event.type === "error") {
          currentText += `\n\n*Error: ${event.data.message}*`;
          const lastIdx = currentBlocks.length - 1;
          if (lastIdx >= 0 && currentBlocks[lastIdx].type === "text") {
            currentBlocks[lastIdx] = { type: "text", content: currentText };
          } else {
            currentBlocks.push({ type: "text", content: currentText });
          }
          updateMsg();
        }
      }, workspace, model, conversationId ?? undefined);

      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId ? { ...m, loading: false } : m,
        ),
      );
      onMessageSent?.();
      refreshConversations();
    } catch (e) {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId
            ? {
                ...m,
                loading: false,
                content: `Failed to get response: ${e instanceof Error ? e.message : "unknown error"}. Is the API running?`,
              }
            : m,
        ),
      );
    } finally {
      setIsLoading(false);
    }
  }, [input, isLoading, messages, questionId, onMessageSent, onNodeRef, workspace, onShowReview, conversationId, model, refreshConversations]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSubmit();
      }
    },
    [handleSubmit],
  );

  const handleTextareaChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      setInput(e.target.value);
      const el = e.target;
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 120) + "px";
    },
    [],
  );

  return (
    <div className={`chat-panel ${isOpen ? "chat-open" : "chat-closed"}`}>
      {!isOpen && (
        <button
          className="chat-toggle-strip"
          onClick={onToggle}
          title="Open chat (\u2318/)"
        >
          <span className="chat-toggle-label">Chat</span>
          <span className="chat-toggle-shortcut">\u2318/</span>
        </button>
      )}

      {isOpen && (
        <div className="chat-inner">
          <div className="chat-header">
            <div style={{ flex: 1, minWidth: 0 }}>
              <div
                style={{
                  fontFamily: "var(--font-mono-stack)",
                  fontSize: "10px",
                  letterSpacing: "0.1em",
                  textTransform: "uppercase",
                  color: "var(--fg-dim)",
                  marginBottom: "4px",
                }}
              >
                Chat
                <span style={{ marginLeft: "8px", color: "var(--fg-dim)", fontSize: "9px", letterSpacing: "0.04em" }}>
                  {model}
                </span>
              </div>
              <div
                style={{
                  fontSize: "13px",
                  color: "var(--fg-muted)",
                  lineHeight: 1.3,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {questionHeadline}
              </div>
            </div>
            <button
              onClick={() => setShowSidebar((s) => !s)}
              className="chat-close-btn"
              title="Toggle conversation list"
              style={{ marginRight: "4px" }}
            >
              {showSidebar ? "hide" : "history"}
            </button>
            <button
              onClick={handleNewChat}
              className="chat-close-btn"
              title="Start a new conversation"
              style={{ marginRight: "4px" }}
            >
              new
            </button>
            <button
              onClick={onToggle}
              className="chat-close-btn"
              title="Close chat (\u2318/)"
            >
              close
            </button>
          </div>

          {showSidebar && (
            <div
              style={{
                borderBottom: "1px solid var(--border)",
                maxHeight: "220px",
                overflowY: "auto",
                padding: "6px 10px",
                fontFamily: "var(--font-mono-stack)",
                fontSize: "11px",
              }}
            >
              {conversations.length === 0 ? (
                <div style={{ color: "var(--fg-dim)", padding: "6px 0" }}>
                  No past conversations in this project.
                </div>
              ) : (
                conversations.map((c) => (
                  <div
                    key={c.id}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "6px",
                      padding: "4px 0",
                      borderBottom: "1px dotted var(--border)",
                      opacity: c.id === conversationId ? 1 : 0.75,
                    }}
                  >
                    <button
                      onClick={() => handleLoadConversation(c.id)}
                      style={{
                        flex: 1,
                        minWidth: 0,
                        textAlign: "left",
                        background: "transparent",
                        border: 0,
                        color: c.id === conversationId ? "var(--accent)" : "var(--fg-muted)",
                        cursor: "pointer",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                        padding: "2px 0",
                      }}
                      title={c.title}
                    >
                      {c.title || "(untitled)"}
                    </button>
                    <button
                      onClick={() => handleRenameConversation(c.id, c.title)}
                      style={{
                        background: "transparent",
                        border: 0,
                        color: "var(--fg-dim)",
                        cursor: "pointer",
                        fontSize: "10px",
                      }}
                      title="Rename"
                    >
                      rename
                    </button>
                    <button
                      onClick={() => handleDeleteConversation(c.id)}
                      style={{
                        background: "transparent",
                        border: 0,
                        color: "var(--fg-dim)",
                        cursor: "pointer",
                        fontSize: "10px",
                      }}
                      title="Delete"
                    >
                      del
                    </button>
                  </div>
                ))
              )}
            </div>
          )}

          <div className="chat-messages">
            {messages.map((msg) => (
              <MessageEntry key={msg.id} message={msg} onNodeRef={onNodeRef} />
            ))}
            <div ref={messagesEndRef} />
          </div>

          <div className="chat-input-area" style={{ position: "relative" }}>
            <SlashCommandDropdown
              input={input}
              cursorPosition={input.length}
              onSelect={handleSlashSelect}
              visible={showDropdown}
              onDismiss={handleDismiss}
            />
            <textarea
              ref={textareaRef}
              value={input}
              onChange={handleTextareaChange}
              onKeyDown={handleKeyDown}
              placeholder="Ask about this research..."
              className="chat-textarea"
              rows={1}
            />
            <button
              onClick={handleSubmit}
              disabled={!input.trim() || isLoading}
              className="chat-send-btn"
            >
              {isLoading ? "..." : "\u21B5"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
