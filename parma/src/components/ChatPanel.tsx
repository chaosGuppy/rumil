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
  branchChatConversation,
  fetchPageByShortId,
} from "@/lib/api";
import type { ChatToolUse, ChatConversationSummary, ChatTurnCosts, NavigateDirective } from "@/lib/api";
import { SlashCommandDropdown, useSlashCommands, recordRecentCommand, COMMANDS } from "./SlashCommands";
import { processChildren } from "./NodeRefLink";
import { useInspectPanel } from "./InspectPanelContext";

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
  costs?: ChatTurnCosts;
  // Full id of the question this turn was asked against. When it doesn't
  // match the ChatPanel's current `questionId`, the message renders a small
  // "(on question: <headline>)" tag so the user can see the context shifted.
  // `null` means "no question scope" (landing-page chat).
  questionId?: string | null;
  // The persisted `chat_messages.seq` value for this message, if any. Needed
  // for the "branch from here" action — the backend branches on seq, not on
  // message id. Absent for the ephemeral greeting/system messages and for
  // in-flight assistant messages that haven't been saved yet.
  seq?: number;
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
  openRunId?: string;
  openPageIds?: string[];
  viewMode?: string;
  openCallId?: string;
  drawerPageId?: string;
  activeSection?: string;
  reviewOpen?: boolean;
  // Invoked when a `tool_use_result` event carries a `__navigate__` directive
  // (emitted by the backend `set_view` tool). Hosts should update their URL
  // params to match the directive; see `applyNavigate` in page.tsx.
  onNavigate?: (directive: NavigateDirective) => void;
}

function contentToText(content: unknown): string {
  if (typeof content === "string") return content;
  if (content && typeof content === "object" && "text" in content) {
    return String((content as { text: string }).text);
  }
  return "";
}

function persistedMessagesToUi(
  raw: Array<{ id: string; role: string; content: Record<string, unknown>; seq: number; ts: string; question_id?: string | null }>,
): Message[] {
  const out: Message[] = [];
  for (const m of raw) {
    if (m.role === "user") {
      out.push({
        id: m.id,
        role: "user",
        content: contentToText(m.content),
        timestamp: new Date(m.ts),
        questionId: m.question_id ?? null,
        seq: m.seq,
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
      const costsRaw = m.content?.costs as
        | { chat_usd?: unknown; research_usd?: unknown; research_by_call_type?: unknown }
        | undefined;
      const costs: ChatTurnCosts | undefined = costsRaw
        ? {
            chat_usd: typeof costsRaw.chat_usd === "number" ? costsRaw.chat_usd : 0,
            research_usd:
              typeof costsRaw.research_usd === "number" ? costsRaw.research_usd : 0,
            research_by_call_type:
              (costsRaw.research_by_call_type as Record<string, number>) ?? {},
          }
        : undefined;
      out.push({
        id: m.id,
        role: "assistant",
        content: textAccum,
        timestamp: new Date(m.ts),
        blocks,
        costs,
        questionId: m.question_id ?? null,
        seq: m.seq,
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

function formatUsd(v: number): string {
  if (v === 0) return "$0";
  if (v < 0.001) return "<$0.001";
  return `$${v.toFixed(3)}`;
}

// Map internal rumil call types to user-facing labels for the cost footer.
// chat_direct is dropped entirely (tiny mutation-move cost, shows up for
// every mutation); scout_* / find_considerations / assess / web_research
// collapse to "research call" so users see one line per kind of work
// rather than the internal taxonomy.
function labelForCallType(ct: string): string | null {
  if (ct === "chat_direct") return null;
  if (ct === "ingest") return "ingest";
  if (
    ct === "find_considerations" ||
    ct === "assess" ||
    ct === "web_research" ||
    ct.startsWith("scout_")
  ) {
    return "research call";
  }
  return ct;
}

function summarizeResearchByType(byType: Record<string, number>): {
  total: number;
  parts: string[];
} {
  const byLabel: Record<string, { usd: number; count: number }> = {};
  let total = 0;
  for (const [ct, usd] of Object.entries(byType)) {
    const label = labelForCallType(ct);
    if (label === null) continue;
    total += usd;
    const entry = byLabel[label] ?? { usd: 0, count: 0 };
    entry.usd += usd;
    entry.count += 1;
    byLabel[label] = entry;
  }
  const parts = Object.entries(byLabel).map(([label, { usd, count }]) => {
    const plural = count === 1 ? "" : "s";
    return `${count} ${label}${plural} ${formatUsd(usd)}`;
  });
  return { total, parts };
}

function CostFooter({ costs }: { costs: ChatTurnCosts }) {
  const { total: researchDisplay, parts } = summarizeResearchByType(
    costs.research_by_call_type,
  );
  const showResearch = researchDisplay > 0 || parts.length > 0;
  return (
    <div
      style={{
        marginTop: "6px",
        fontFamily: "var(--font-mono-stack)",
        fontSize: "9px",
        letterSpacing: "0.02em",
        color: "var(--fg-dim)",
      }}
    >
      <span>Chat: {formatUsd(costs.chat_usd)}</span>
      {showResearch && (
        <span>
          {" \u00b7 Research: "}
          {formatUsd(researchDisplay)}
          {parts.length > 0 ? ` (${parts.join(", ")})` : ""}
        </span>
      )}
    </div>
  );
}

// "Searching…" verb tailored to the tool name. Non-committal for unknown
// tools so we don't lie about what's happening.
function runningVerb(name: string): string {
  if (name.includes("search")) return "searching";
  if (name.includes("inspect") || name.includes("page")) return "loading";
  if (name.includes("dispatch") || name.includes("orchestrat")) return "dispatching";
  if (name.includes("ingest")) return "ingesting";
  if (name.includes("create_question") || name.includes("ask")) return "adding";
  return "running";
}

function ToolBlock({ tu }: { tu: ChatToolUse }) {
  const isRunning = !tu.result;
  if (isRunning) {
    // Backend streams orchestrator_progress SSE events into tu.input._progress
    // for long-running dispatches (find_considerations / research / etc.).
    // Show it verbatim when present so the user sees step-by-step progress
    // instead of an inscrutable spinner.
    const progress =
      typeof tu.input?._progress === "string" ? tu.input._progress : null;
    return (
      <div className="chat-tool-running">
        <span className="chat-tool-dot" aria-hidden="true" />
        <span className="chat-tool-name">{tu.name}</span>
        <span className="chat-tool-status">
          {progress ? `\u2014 ${progress}` : `${runningVerb(tu.name)}\u2026`}
        </span>
      </div>
    );
  }
  return (
    <div className="chat-tool-done">
      <span className="chat-tool-check" aria-hidden="true">{"\u2713"}</span>
      <span className="chat-tool-name">{tu.name}</span>
      {tu.result && (
        <span className="chat-tool-result">
          {` \u2014 ${tu.result.slice(0, 80)}`}
        </span>
      )}
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
  currentQuestionId,
  headlineFor,
  onBranch,
}: {
  message: Message;
  onNodeRef?: (id: string) => void;
  currentQuestionId?: string;
  headlineFor?: (qid: string) => string | undefined;
  // Called with the message's seq when the user clicks the "branch from here"
  // affordance. Absent => no branching UI (e.g. for the ephemeral greeting,
  // which has no persisted seq).
  onBranch?: (seq: number) => void;
}) {
  const [hovered, setHovered] = useState(false);
  const isUser = message.role === "user";
  const blocks = message.blocks;
  const canBranch =
    typeof message.seq === "number" && !message.loading && !!onBranch;

  // Surface "this turn was asked against a different question" so the
  // user sees the context shifted when browsing across questions in a
  // single project conversation. The check is defensive: skip if either
  // side is missing, and skip when they match.
  const offQuestion =
    message.questionId &&
    currentQuestionId &&
    message.questionId !== currentQuestionId
      ? message.questionId
      : null;
  const offHeadline =
    offQuestion && headlineFor ? headlineFor(offQuestion) : undefined;

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{ padding: "12px 0", borderBottom: "1px solid var(--border)", position: "relative" }}
    >
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
        {offQuestion && (
          <span
            title={offHeadline ?? offQuestion}
            style={{
              fontFamily: "var(--font-mono-stack)",
              fontSize: "9px",
              color: "var(--fg-dim)",
              letterSpacing: "0.02em",
              fontStyle: "italic",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              maxWidth: "60%",
            }}
          >
            {`(on question: ${offHeadline ?? offQuestion.slice(0, 8)})`}
          </span>
        )}
        {canBranch && (
          <button
            type="button"
            className="chat-branch-btn"
            onClick={() => onBranch?.(message.seq as number)}
            title="Branch a new conversation from this message"
            style={{
              marginLeft: "auto",
              opacity: hovered ? 1 : 0,
              pointerEvents: hovered ? "auto" : "none",
            }}
          >
            {"\u21AA branch"}
          </button>
        )}
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

      {!isUser && !message.loading && message.costs && (
        <CostFooter costs={message.costs} />
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
  openRunId,
  openPageIds,
  viewMode,
  openCallId,
  drawerPageId,
  activeSection,
  reviewOpen,
  onNavigate,
}: ChatPanelProps) {
  const initialAssistantMessage: Message = {
    id: "initial",
    role: "assistant",
    content:
      "Ask me about this view \u2014 I can explain the reasoning behind claims, surface tensions between findings, or discuss what the research might be missing. Or use `/` for slash commands.",
    timestamp: new Date(),
  };
  const { openInspect } = useInspectPanel();
  // Final node-ref handler — prefer the prop (parent may scroll the view too)
  // but always fall back to the global inspect panel so clicks never no-op.
  const handleNodeRef = useCallback(
    (id: string) => {
      if (onNodeRef) onNodeRef(id);
      else openInspect(id);
    },
    [onNodeRef, openInspect],
  );

  const [messages, setMessages] = useState<Message[]>([initialAssistantMessage]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<ChatConversationSummary[]>([]);
  const [showSidebar, setShowSidebar] = useState(false);
  const [input, setInput] = useState("");
  // Branch metadata for the currently-loaded conversation. `null` when this
  // conversation is a root (not a branch). When set, we render a "branched
  // from …" badge in the chat header so the user always knows they're in a
  // fork. The `parentTitle` is resolved from the conversations list when we
  // have it; otherwise we fall back to the short id.
  const [branchedFrom, setBranchedFrom] = useState<{
    parentId: string;
    atSeq: number;
  } | null>(null);
  // Cache full question_id → headline for off-question message tags. We
  // lazily populate via fetchPageByShortId the first time we see an unknown
  // question id in a persisted transcript. Keeping it in a ref avoids
  // re-renders on cache writes — we only re-render when a message list
  // change triggers a resolution pass.
  const [headlineCache, setHeadlineCache] = useState<Record<string, string>>({});
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // When the current question's headline is passed in as a prop, seed the
  // cache so the on-question label for that id resolves immediately.
  useEffect(() => {
    if (!questionId) return;
    setHeadlineCache((prev) =>
      prev[questionId] === questionHeadline ? prev : { ...prev, [questionId]: questionHeadline },
    );
  }, [questionId, questionHeadline]);

  // Resolve any unknown question ids referenced by loaded messages. Off-
  // question tags render a short id + no headline until this completes;
  // once resolved, the tag text upgrades.
  useEffect(() => {
    const unknown = new Set<string>();
    for (const m of messages) {
      const qid = m.questionId;
      if (qid && !(qid in headlineCache)) unknown.add(qid);
    }
    if (unknown.size === 0) return;
    let cancelled = false;
    (async () => {
      const updates: Record<string, string> = {};
      await Promise.all(
        Array.from(unknown).map(async (qid) => {
          try {
            const page = await fetchPageByShortId(qid.slice(0, 8));
            if (page && page.headline) updates[qid] = page.headline;
          } catch {
            /* leave it unresolved — renders fall back to short id */
          }
        }),
      );
      if (cancelled || Object.keys(updates).length === 0) return;
      setHeadlineCache((prev) => ({ ...prev, ...updates }));
    })();
    return () => {
      cancelled = true;
    };
  }, [messages, headlineCache]);

  const headlineFor = useCallback(
    (qid: string) => headlineCache[qid],
    [headlineCache],
  );

  const refreshConversations = useCallback(async () => {
    if (!projectId) return;
    try {
      // Project-scoped: conversations can span multiple questions, so we
      // don't filter by questionId here. See `loadedForKeyRef` below.
      const items = await listChatConversations(projectId);
      setConversations(items);
    } catch {
      /* ignore — API may not be available yet */
    }
  }, [projectId]);

  useEffect(() => {
    if (isOpen) refreshConversations();
  }, [isOpen, refreshConversations]);

  // Auto-bind the chat to the most-recent conversation in this project.
  // Runs once per project change. Question switches within the same
  // project deliberately do NOT reset the transcript — the conversation
  // is project-scoped, and per-message question_id tags mark turns that
  // referred to a different question than the current one.
  const loadedForKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (!projectId) return;
    const key = projectId;
    if (loadedForKeyRef.current === key) return;
    loadedForKeyRef.current = key;
    let cancelled = false;
    (async () => {
      try {
        const items = await listChatConversations(projectId);
        if (cancelled) return;
        if (items.length === 0) {
          // No prior conversation in this project — keep the initial
          // greeting and let the first message auto-create the row.
          setConversationId(null);
          setMessages([initialAssistantMessage]);
          return;
        }
        const latest = items[0]; // backend already orders by updated_at desc
        const detail = await getChatConversation(latest.id);
        if (cancelled) return;
        const ui = persistedMessagesToUi(detail.messages);
        setConversationId(latest.id);
        setMessages(ui.length ? ui : [initialAssistantMessage]);
        setBranchedFrom(
          detail.parent_conversation_id && typeof detail.branched_at_seq === "number"
            ? { parentId: detail.parent_conversation_id, atSeq: detail.branched_at_seq }
            : null,
        );
      } catch {
        /* API may be unavailable; leave state untouched */
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const handleNewChat = useCallback(() => {
    setConversationId(null);
    setMessages([initialAssistantMessage]);
    setShowSidebar(false);
    setBranchedFrom(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleLoadConversation = useCallback(async (id: string) => {
    try {
      const detail = await getChatConversation(id);
      const uiMessages = persistedMessagesToUi(detail.messages);
      setMessages(uiMessages.length ? uiMessages : [initialAssistantMessage]);
      setConversationId(id);
      setShowSidebar(false);
      setBranchedFrom(
        detail.parent_conversation_id && typeof detail.branched_at_seq === "number"
          ? { parentId: detail.parent_conversation_id, atSeq: detail.branched_at_seq }
          : null,
      );
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

  // Branch a new conversation from a specific message seq in the current
  // conversation. The parent is preserved; we swap the active chat to the
  // new (forked) one and render its truncated transcript.
  //
  // Intentional non-goal: UI state (pinned panes, view mode, inspect drawer,
  // open run/call, active section, review panel) is NOT re-applied to the
  // branch. Branching is a CONVERSATION-content fork — not a full session
  // restore. If the user wants to reproduce the surrounding UI state too,
  // they can scroll back in the parent and set it manually. Keeping this
  // narrow avoids surprising "why did my panes change" moments and keeps
  // the feature's contract small.
  const handleBranchFromMessage = useCallback(
    async (atSeq: number) => {
      if (!conversationId) return;
      if (
        typeof window !== "undefined" &&
        !window.confirm(
          `Branch a new conversation from this message (seq ${atSeq})?\n\n` +
            "The original conversation is preserved. You'll continue in the new branch.",
        )
      ) {
        return;
      }
      try {
        const fresh = await branchChatConversation(conversationId, atSeq);
        const uiMessages = persistedMessagesToUi(fresh.messages);
        setMessages(uiMessages.length ? uiMessages : [initialAssistantMessage]);
        setConversationId(fresh.id);
        setBranchedFrom(
          fresh.parent_conversation_id && typeof fresh.branched_at_seq === "number"
            ? { parentId: fresh.parent_conversation_id, atSeq: fresh.branched_at_seq }
            : null,
        );
        await refreshConversations();
      } catch (e) {
        console.error("Failed to branch conversation", e);
        if (typeof window !== "undefined") {
          window.alert(
            e instanceof Error
              ? `Branch failed: ${e.message}`
              : "Branch failed (see console for details).",
          );
        }
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [conversationId, refreshConversations],
  );

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

  const seedSlashCommand = useCallback(
    (prefix: string) => {
      setInput(prefix);
      requestAnimationFrame(() => {
        const el = textareaRef.current;
        if (!el) return;
        el.focus();
        el.setSelectionRange(prefix.length, prefix.length);
        el.style.height = "auto";
        el.style.height = Math.min(el.scrollHeight, 120) + "px";
      });
    },
    [],
  );

  const isFreshChat =
    messages.length === 1 && messages[0]?.id === "initial" && !isLoading;

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
      recordRecentCommand("review");
      onShowReview?.();
      return;
    }

    if (trimmed.startsWith("/inspect")) {
      const arg = trimmed.slice("/inspect".length).trim();
      const match = arg.match(/\b([0-9a-f]{8})\b/i);
      setInput("");
      recordRecentCommand("inspect");
      if (!arg) {
        setMessages((prev) => [
          ...prev,
          {
            id: `sys-${Date.now()}`,
            role: "assistant",
            content: "Usage: `/inspect <page_id>` — e.g. `/inspect f8a1b2c3`.",
            timestamp: new Date(),
          },
        ]);
        return;
      }
      if (!match) {
        setMessages((prev) => [
          ...prev,
          {
            id: `sys-${Date.now()}`,
            role: "assistant",
            content:
              `No valid short id in \`${arg}\`. Expected an 8-character hex id (e.g. \`f8a1b2c3\`).`,
            timestamp: new Date(),
          },
        ]);
        return;
      }
      const shortId = match[1].toLowerCase();
      try {
        const page = await fetchPageByShortId(shortId);
        if (page) {
          openInspect(shortId);
          setMessages((prev) => [
            ...prev,
            {
              id: `sys-${Date.now()}`,
              role: "assistant",
              content: `Opened inspect panel for ${shortId}.`,
              timestamp: new Date(),
            },
          ]);
        } else {
          setMessages((prev) => [
            ...prev,
            {
              id: `sys-${Date.now()}`,
              role: "assistant",
              content:
                `No page found for \`${shortId}\`. It may be in a staged run you don\u2019t have visibility into, or the id may be mistyped.`,
              timestamp: new Date(),
            },
          ]);
        }
      } catch (e) {
        setMessages((prev) => [
          ...prev,
          {
            id: `sys-${Date.now()}`,
            role: "assistant",
            content:
              `Failed to resolve \`${shortId}\`: ${e instanceof Error ? e.message : "unknown error"}.`,
            timestamp: new Date(),
          },
        ]);
      }
      return;
    }

    if (trimmed.startsWith("/")) {
      const cmdName = trimmed.slice(1).split(/\s+/)[0]?.toLowerCase();
      // Unknown slashes silently forwarded to the model look like a no-op
      // from the user's side (ux-review-wave7 #3). Surface an explicit
      // "unknown command" reply in the transcript so the interaction
      // closes the loop. Known commands — even the ones handled by the
      // model via tool use (/search, /ask, /dispatch, /orchestrate,
      // /ingest) — still fall through to the stream as before.
      const known = COMMANDS.some((c) => c.name === cmdName);
      if (cmdName && !known) {
        setInput("");
        setMessages((prev) => [
          ...prev,
          {
            id: `user-${Date.now()}`,
            role: "user",
            content: trimmed,
            timestamp: new Date(),
          },
          {
            id: `sys-${Date.now()}`,
            role: "assistant",
            content:
              `Unknown command \`/${cmdName}\`. Type \`/\` to see the available slash commands.`,
            timestamp: new Date(),
          },
        ]);
        return;
      }
      if (cmdName) recordRecentCommand(cmdName);
    }

    const userMsg: Message = {
      id: `user-${Date.now()}`,
      role: "user",
      content: trimmed,
      timestamp: new Date(),
      questionId: questionId || null,
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
        questionId: questionId || null,
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
          // set_view (and any other tool that wants to move the UI) can
          // return a JSON payload with a `__navigate__` directive. Parse
          // each tool result defensively — non-JSON or unrelated tools
          // are the common case, so parse failures silently no-op.
          if (onNavigate && typeof result === "string" && result.includes("__navigate__")) {
            try {
              const parsed = JSON.parse(result) as { __navigate__?: NavigateDirective };
              if (parsed?.__navigate__ && typeof parsed.__navigate__.view === "string") {
                onNavigate(parsed.__navigate__);
              }
            } catch {
              /* not JSON — ignore */
            }
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
        } else if (event.type === "turn_costs") {
          const costs: ChatTurnCosts = {
            chat_usd: Number(event.data.chat_usd ?? 0),
            research_usd: Number(event.data.research_usd ?? 0),
            research_by_call_type:
              (event.data.research_by_call_type as Record<string, number>) ?? {},
          };
          setMessages((prev) =>
            prev.map((m) => (m.id === assistantId ? { ...m, costs } : m)),
          );
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
      }, workspace, model, conversationId ?? undefined, {
        openRunId,
        openPageIds,
        viewMode,
        openCallId,
        drawerPageId,
        activeSection,
        reviewOpen,
      });

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
  }, [input, isLoading, messages, questionId, onMessageSent, onNodeRef, workspace, onShowReview, conversationId, model, refreshConversations, openInspect, openRunId, openPageIds, viewMode, openCallId, drawerPageId, activeSection, reviewOpen]);

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
          title="Open chat (⌘/)"
        >
          <span className="chat-toggle-label">Chat</span>
          <span className="chat-toggle-shortcut">⌘/</span>
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
              {branchedFrom && (() => {
                const parent = conversations.find((c) => c.id === branchedFrom.parentId);
                const parentLabel = parent?.title || branchedFrom.parentId.slice(0, 8);
                return (
                  <button
                    type="button"
                    className="chat-branched-badge"
                    title={
                      parent?.title
                        ? `Branched from "${parent.title}" at message ${branchedFrom.atSeq}. Click to open parent.`
                        : `Branched from ${branchedFrom.parentId.slice(0, 8)} at message ${branchedFrom.atSeq}. Click to open parent.`
                    }
                    onClick={() => handleLoadConversation(branchedFrom.parentId)}
                  >
                    {"\u21AA "}branched from {parentLabel} @ {branchedFrom.atSeq}
                  </button>
                );
              })()}
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
              title="Close chat (⌘/)"
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
                      title={
                        c.parent_conversation_id
                          ? `Branched from ${c.parent_conversation_id.slice(0, 8)} @ ${c.branched_at_seq ?? "?"}`
                          : c.title
                      }
                    >
                      {c.parent_conversation_id ? "\u21AA " : ""}
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
              <MessageEntry
                key={msg.id}
                message={msg}
                onNodeRef={handleNodeRef}
                currentQuestionId={questionId || undefined}
                headlineFor={headlineFor}
                onBranch={handleBranchFromMessage}
              />
            ))}
            {isFreshChat && (
              <div className="chat-starter-chips" role="group" aria-label="Starter prompts">
                <div className="chat-starter-chips-label">Starters</div>
                <div className="chat-starter-chips-row">
                  <button
                    type="button"
                    className="chat-starter-chip"
                    onClick={() => seedSlashCommand("/search")}
                    title="Search the workspace for relevant research"
                  >
                    <span className="chat-starter-chip-cmd">/search</span>
                    <span className="chat-starter-chip-desc">Find related pages</span>
                  </button>
                  <button
                    type="button"
                    className="chat-starter-chip"
                    onClick={() => seedSlashCommand("/ask")}
                    title="Add a new research question to the workspace"
                  >
                    <span className="chat-starter-chip-cmd">/ask</span>
                    <span className="chat-starter-chip-desc">Add a question to investigate</span>
                  </button>
                  <button
                    type="button"
                    className="chat-starter-chip"
                    onClick={() => {
                      setInput("");
                      onShowReview?.();
                    }}
                    title="Show pending suggestions in the review queue"
                  >
                    <span className="chat-starter-chip-cmd">/review</span>
                    <span className="chat-starter-chip-desc">Show pending suggestions</span>
                  </button>
                </div>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>

          <div className="chat-input-area" style={{ position: "relative" }}>
            <SlashCommandDropdown
              input={input}
              cursorPosition={input.length}
              onSelect={handleSlashSelect}
              visible={showDropdown}
              onDismiss={handleDismiss}
              activeModel={model}
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
