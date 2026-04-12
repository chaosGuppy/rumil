"use client";

import { useState, useRef, useEffect, useCallback } from "react";

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
  toolUses?: { name: string; input: Record<string, unknown>; result: string }[];
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8099";

interface ChatPanelProps {
  questionHeadline: string;
  isOpen: boolean;
  onToggle: () => void;
}

function formatTime(date: Date): string {
  return date.toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  });
}

function MessageEntry({ message }: { message: Message }) {
  const isUser = message.role === "user";

  return (
    <div
      style={{
        padding: "12px 0",
        borderBottom: "1px solid var(--border)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          gap: "8px",
          marginBottom: "4px",
        }}
      >
        <span
          style={{
            fontFamily: "var(--font-mono-stack)",
            fontSize: "10px",
            letterSpacing: "0.06em",
            textTransform: "uppercase",
            color: isUser ? "var(--accent)" : "var(--node-claim)",
            fontWeight: 500,
          }}
        >
          {isUser ? "You" : "Rumil"}
        </span>
        <span
          style={{
            fontFamily: "var(--font-mono-stack)",
            fontSize: "9px",
            color: "var(--fg-dim)",
            letterSpacing: "0.02em",
          }}
        >
          {formatTime(message.timestamp)}
        </span>
      </div>
      <div
        style={{
          fontSize: "14px",
          lineHeight: 1.6,
          color: isUser ? "var(--fg)" : "var(--fg)",
          fontFamily: "var(--font-body-stack)",
          borderLeft: isUser ? "none" : "2px solid var(--border)",
          paddingLeft: isUser ? "0" : "10px",
        }}
      >
        {message.content.split("\n").map((line, i) => (
          <p
            key={i}
            style={{
              margin: i === 0 ? "0" : "6px 0 0 0",
            }}
          >
            {line}
          </p>
        ))}
      </div>
      {message.toolUses && message.toolUses.length > 0 && (
        <div
          style={{
            marginTop: "8px",
            fontFamily: "var(--font-mono-stack)",
            fontSize: "10px",
            color: "var(--fg-dim)",
            letterSpacing: "0.02em",
          }}
        >
          {message.toolUses.map((tu, i) => (
            <div key={i} style={{ padding: "2px 0" }}>
              used {tu.name}({Object.values(tu.input).join(", ")})
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function ChatPanel({
  questionHeadline,
  isOpen,
  onToggle,
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

  const handleSubmit = useCallback(async () => {
    const trimmed = input.trim();
    if (!trimmed || isLoading) return;

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

    try {
      const apiMessages = [...messages, userMsg]
        .filter((m) => m.id !== "initial")
        .map((m) => ({ role: m.role, content: m.content }));

      const res = await fetch(`${API_BASE}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question_id: questionHeadline,
          messages: apiMessages,
          workspace: "default",
        }),
      });

      if (!res.ok) {
        throw new Error(`API error: ${res.status}`);
      }

      const data = await res.json();
      const assistantMsg: Message = {
        id: `asst-${Date.now()}`,
        role: "assistant",
        content: data.response,
        timestamp: new Date(),
        toolUses: data.tool_uses,
      };
      setMessages((prev) => [...prev, assistantMsg]);
    } catch (e) {
      const errorMsg: Message = {
        id: `err-${Date.now()}`,
        role: "assistant",
        content: `Failed to get response: ${e instanceof Error ? e.message : "unknown error"}. Is the API running?`,
        timestamp: new Date(),
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setIsLoading(false);
    }
  }, [input, isLoading, messages, questionHeadline]);

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
              <MessageEntry key={msg.id} message={msg} />
            ))}
            <div ref={messagesEndRef} />
          </div>

          {/* input */}
          <div className="chat-input-area">
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
