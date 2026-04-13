"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { RunPreview } from "./RunPreview";
import { SlashCommandDropdown, useSlashCommands } from "./SlashCommands";

interface ToolUse {
  name: string;
  input: Record<string, unknown>;
  result: string;
}

type MessageBlock =
  | { type: "text"; content: string }
  | { type: "tool"; tool: ToolUse };

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string; // full text (for apiMessages compat)
  timestamp: Date;
  streaming?: boolean;
  blocks?: MessageBlock[];
}

interface SSEEvent {
  type: string;
  data: Record<string, unknown>;
}

function* parseSSE(raw: string): Generator<SSEEvent> {
  for (const block of raw.split("\n\n")) {
    const lines = block.split("\n");
    let type = "";
    let data = "";
    for (const line of lines) {
      if (line.startsWith("event: ")) type = line.slice(7);
      else if (line.startsWith("data: ")) data = line.slice(6);
    }
    if (type && data) {
      try {
        yield { type, data: JSON.parse(data) };
      } catch {
        /* skip malformed */
      }
    }
  }
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8099";

interface ChatPanelProps {
  questionHeadline: string;
  isOpen: boolean;
  onToggle: () => void;
  onMessageSent?: () => void;
  onNodeRef?: (nodeId: string) => void;
  onShowReview?: () => void;
  workspace?: string;
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

function tryParsePreview(result: string) {
  try {
    const data = JSON.parse(result);
    if (data.scope_node && data.context_nodes) return data;
  } catch { /* not JSON or not a preview */ }
  return null;
}

function ToolBlock({ tu, onAction, onNodeRef }: {
  tu: ToolUse;
  onAction?: (text: string) => void;
  onNodeRef?: (id: string) => void;
}) {
  if (tu.name === "preview_run" && tu.result) {
    const preview = tryParsePreview(tu.result);
    if (preview) {
      return <RunPreview data={preview} onAction={onAction} onNodeRef={onNodeRef} />;
    }
  }
  if (tu.name === "run_orchestrator" && tu.result) {
    return (
      <details className="rp-orch-result">
        <summary>✓ {tu.name} — {tu.result.split("\n")[0].slice(0, 100)}</summary>
        <pre className="rp-orch-detail">{tu.result}</pre>
      </details>
    );
  }
  const progress = (tu.input._progress as string) || "";
  return (
    <div style={{ padding: "2px 0" }}>
      {tu.result ? "✓" : "⟳"} {tu.name}
      {tu.result ? ` — ${tu.result.slice(0, 80)}` : progress ? ` — ${progress}` : " …"}
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
  onAction,
}: {
  message: Message;
  onNodeRef?: (id: string) => void;
  onAction?: (text: string) => void;
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
                <ToolBlock tu={block.tool} onAction={onAction} onNodeRef={onNodeRef} />
              </div>
            ),
          )}
        </div>
      ) : message.content ? (
        <div style={{ borderLeft: "2px solid var(--border)", paddingLeft: "10px" }}>
          <TextContent text={message.content} onNodeRef={onNodeRef} />
        </div>
      ) : null}

      {message.streaming && !message.content && (!blocks || blocks.length === 0) && (
        <div className="thinking-indicator" style={{ marginTop: "4px" }}>
          <span className="thinking-dot" />
          <span className="thinking-text">thinking</span>
        </div>
      )}
    </div>
  );
}

export function ChatPanel({
  questionHeadline,
  isOpen,
  onToggle,
  onMessageSent,
  onNodeRef,
  onShowReview,
  workspace = "default",
}: ChatPanelProps) {
  const [messages, setMessages] = useState<Message[]>([
    {
      id: "initial",
      role: "assistant",
      content:
        "Ask me about this worldview — I can explain the reasoning behind claims, surface tensions between findings, or discuss what the research might be missing.",
      timestamp: new Date(),
    },
  ]);
  const [input, setInput] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

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
          role: "assistant",
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
        streaming: true,
        blocks: [],
      },
    ]);

    try {
      const apiMessages = [...messages, userMsg]
        .filter((m) => m.id !== "initial")
        .map((m) => ({ role: m.role, content: m.content }));

      const res = await fetch(`${API_BASE}/api/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question_id: questionHeadline,
          messages: apiMessages,
          workspace,
          model,
        }),
      });

      if (!res.ok) {
        throw new Error(`API error: ${res.status}`);
      }

      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      let fullText = "";
      let currentBlocks: MessageBlock[] = [];
      let buffer = "";

      const updateMessage = () => {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? { ...m, content: fullText, blocks: [...currentBlocks] }
              : m,
          ),
        );
      };

      // Ensure the last block is a text block, return it
      const ensureTextBlock = (): MessageBlock & { type: "text" } => {
        const last = currentBlocks[currentBlocks.length - 1];
        if (last && last.type === "text") return last;
        const block: MessageBlock = { type: "text", content: "" };
        currentBlocks = [...currentBlocks, block];
        return block;
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const events = [...parseSSE(buffer)];
        const lastDoubleNewline = buffer.lastIndexOf("\n\n");
        buffer = lastDoubleNewline >= 0 ? buffer.slice(lastDoubleNewline + 2) : buffer;

        for (const event of events) {
          if (event.type === "text") {
            const chunk = event.data.content as string;
            fullText += chunk;
            const textBlock = ensureTextBlock();
            textBlock.content += chunk;
            updateMessage();
          } else if (event.type === "tool_use_start") {
            const tool: ToolUse = { name: event.data.name as string, input: {}, result: "" };
            currentBlocks = [...currentBlocks, { type: "tool", tool }];
            updateMessage();
          } else if (event.type === "tool_use_result") {
            const toolName = event.data.name as string;
            const toolResult = event.data.result as string;
            // Find the matching pending tool block and fill in its result
            let matched = false;
            currentBlocks = currentBlocks.map((b) => {
              if (!matched && b.type === "tool" && b.tool.name === toolName && !b.tool.result) {
                matched = true;
                return {
                  type: "tool",
                  tool: {
                    name: b.tool.name,
                    input: (event.data.input as Record<string, unknown>) || {},
                    result: toolResult,
                  },
                };
              }
              return b;
            });
            updateMessage();
            if (toolName === "create_node" || toolName === "run_orchestrator") {
              onMessageSent?.();
              const idMatch = toolResult.match(/node ([0-9a-f]{8})/);
              if (idMatch) onNodeRef?.(idMatch[1]);
            }
          } else if (event.type === "orchestrator_progress") {
            // Update the pending run_orchestrator tool block with progress
            let matched = false;
            currentBlocks = currentBlocks.map((b) => {
              if (!matched && b.type === "tool" && b.tool.name === "run_orchestrator" && !b.tool.result) {
                matched = true;
                return {
                  type: "tool" as const,
                  tool: { ...b.tool, input: { ...b.tool.input, _progress: event.data.message as string } },
                };
              }
              return b;
            });
            updateMessage();
          } else if (event.type === "error") {
            fullText += `\n\n*Error: ${event.data.message}*`;
            const textBlock = ensureTextBlock();
            textBlock.content += `\n\n*Error: ${event.data.message}*`;
            updateMessage();
          }
        }
      }

      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId ? { ...m, streaming: false } : m,
        ),
      );
      onMessageSent?.();
    } catch (e) {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId
            ? {
                ...m,
                streaming: false,
                content:
                  m.content ||
                  `Failed to get response: ${e instanceof Error ? e.message : "unknown error"}. Is the API running?`,
              }
            : m,
        ),
      );
    } finally {
      setIsLoading(false);
    }
  }, [input, isLoading, messages, questionHeadline, onMessageSent, onNodeRef, workspace, model]);

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

  const handleAction = useCallback(
    (text: string) => {
      setInput(text);
      textareaRef.current?.focus();
    },
    [textareaRef],
  );

  return (
    <div className={`chat-panel ${isOpen ? "chat-open" : "chat-closed"}`}>
      {/* collapsed strip */}
      {!isOpen && (
        <button
          className="chat-toggle-strip"
          onClick={onToggle}
          title="Open chat (⌘/)"
        >
          <span className="chat-toggle-label">Chat</span>
          <span className="chat-toggle-shortcut">⌘/</span>
        </button>
      )}

      {/* open panel */}
      {isOpen && (
        <div className="chat-inner">
          {/* header */}
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
              onClick={onToggle}
              className="chat-close-btn"
              title="Close chat (⌘/)"
            >
              close
            </button>
          </div>

          {/* messages */}
          <div className="chat-messages">
            {messages.map((msg) => (
              <MessageEntry key={msg.id} message={msg} onNodeRef={onNodeRef} onAction={handleAction} />
            ))}
            <div ref={messagesEndRef} />
          </div>

          {/* input */}
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
              {isLoading ? "..." : "↵"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
