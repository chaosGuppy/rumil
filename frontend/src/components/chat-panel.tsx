"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePathname } from "next/navigation";
import Link from "next/link";
import type { ConversationListItem } from "@/api";
import { CLIENT_API_BASE } from "@/api-config";
import {
  createConversation,
  deleteConversation,
  getConversation,
  listConversations,
  streamChatTurn,
  subscribeConvEvents,
  type ConvEvent,
  type StreamEvent,
} from "@/lib/chat-api";

const OPEN_KEY = "rumil.chat.open";
const ACTIVE_CONV_KEY = "rumil.chat.active_conv";
const LAST_PROJECT_KEY = "rumil.chat.last_project";
const MODEL_KEY = "rumil.chat.model";

type ModelShort = "haiku" | "sonnet" | "opus";
const MODELS: ModelShort[] = ["haiku", "sonnet", "opus"];
const MODEL_LABEL: Record<ModelShort, string> = {
  haiku: "haiku 4.5",
  sonnet: "sonnet 4.6",
  opus: "opus 4.7",
};
const MODEL_DESC: Record<ModelShort, string> = {
  haiku: "fastest, cheapest",
  sonnet: "balanced default",
  opus: "most capable",
};
const DEFAULT_MODEL: ModelShort = "sonnet";

function isModelShort(v: string): v is ModelShort {
  return MODELS.includes(v as ModelShort);
}

type MessageBlock =
  | { kind: "text"; id: string; text: string }
  | {
      kind: "tool";
      id: string;
      name: string;
      input?: Record<string, unknown>;
      result?: string;
    }
  | {
      kind: "dispatch";
      id: string;
      call_type: string;
      headline: string;
      run_id: string;
      status: "completed" | "failed";
      summary: string;
      trace_url: string;
    };

interface UiMessage {
  id: string;
  role: "user" | "assistant";
  blocks: MessageBlock[];
  seq?: number;
  pending?: boolean;
}

function newId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function pathToQuestionId(pathname: string | null): string | null {
  if (!pathname) return null;
  const m = pathname.match(/^\/pages\/([0-9a-f-]+)/i);
  return m ? m[1] : null;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

async function resolveProjectFromPath(
  pathname: string | null,
): Promise<string | null> {
  if (!pathname) return null;

  const projectMatch = pathname.match(/^\/projects\/([0-9a-f-]+)/i);
  if (projectMatch && UUID_RE.test(projectMatch[1])) {
    return projectMatch[1];
  }

  const pageMatch = pathname.match(/^\/pages\/([0-9a-f-]+)/i);
  if (pageMatch) {
    try {
      const res = await fetch(`${CLIENT_API_BASE}/api/pages/${pageMatch[1]}`);
      if (res.ok) {
        const page = (await res.json()) as { project_id?: string };
        if (page.project_id && UUID_RE.test(page.project_id)) {
          return page.project_id;
        }
      }
    } catch {
      // ignore
    }
  }

  const traceMatch = pathname.match(/^\/traces\/([0-9a-f-]+)/i);
  if (traceMatch) {
    try {
      const res = await fetch(
        `${CLIENT_API_BASE}/api/runs/${traceMatch[1]}/trace-tree`,
      );
      if (res.ok) {
        const tree = (await res.json()) as {
          question?: { project_id?: string } | null;
        };
        const pid = tree.question?.project_id;
        if (pid && UUID_RE.test(pid)) return pid;
      }
    } catch {
      // ignore
    }
  }

  return null;
}

async function fetchFirstProjectId(): Promise<string | null> {
  try {
    const res = await fetch(`${CLIENT_API_BASE}/api/projects`);
    if (!res.ok) return null;
    const projects = (await res.json()) as Array<{ id: string }>;
    return projects[0]?.id ?? null;
  } catch {
    return null;
  }
}

function previewInput(input: Record<string, unknown> | undefined): string {
  if (!input) return "";
  const keys = Object.keys(input);
  if (keys.length === 0) return "";
  const k = keys[0];
  const v = input[k];
  const rendered = typeof v === "string" ? v : JSON.stringify(v);
  const str = rendered.length > 42 ? rendered.slice(0, 40) + "…" : rendered;
  return `${k}=${str}`;
}

function ensureInitialAssistant(messages: UiMessage[]): UiMessage[] {
  const last = messages[messages.length - 1];
  if (last && last.role === "assistant" && last.pending) return messages;
  return [
    ...messages,
    { id: newId(), role: "assistant", blocks: [], pending: true },
  ];
}

function upsertTextDelta(messages: UiMessage[], text: string): UiMessage[] {
  const copy = [...messages];
  const last = copy[copy.length - 1];
  if (!last || last.role !== "assistant") return copy;
  const blocks = [...last.blocks];
  const lastBlock = blocks[blocks.length - 1];
  if (lastBlock && lastBlock.kind === "text") {
    blocks[blocks.length - 1] = {
      ...lastBlock,
      text: lastBlock.text + text,
    };
  } else {
    blocks.push({ kind: "text", id: newId(), text });
  }
  copy[copy.length - 1] = { ...last, blocks };
  return copy;
}

function appendToolChip(
  messages: UiMessage[],
  id: string,
  name: string,
): UiMessage[] {
  const copy = [...messages];
  const last = copy[copy.length - 1];
  if (!last || last.role !== "assistant") return copy;
  const blocks = [...last.blocks, { kind: "tool" as const, id, name }];
  copy[copy.length - 1] = { ...last, blocks };
  return copy;
}

function completeToolChip(
  messages: UiMessage[],
  id: string,
  result: string,
): UiMessage[] {
  return messages.map((m) => {
    if (m.role !== "assistant") return m;
    const blocks = m.blocks.map((b) =>
      b.kind === "tool" && b.id === id ? { ...b, result } : b,
    );
    return { ...m, blocks };
  });
}

function hydrateFromDetail(
  rawMessages: Array<{ [key: string]: unknown }>,
): UiMessage[] {
  const out: UiMessage[] = [];
  for (const raw of rawMessages) {
    const role = typeof raw.role === "string" ? raw.role : "";
    const c =
      raw.content && typeof raw.content === "object"
        ? (raw.content as Record<string, unknown>)
        : {};
    const rawId = typeof raw.id === "string" ? raw.id : newId();
    const seq = typeof raw.seq === "number" ? raw.seq : undefined;
    if (role === "user") {
      const text = typeof c.text === "string" ? c.text : "";
      out.push({
        id: rawId,
        role: "user",
        seq,
        blocks: [{ kind: "text", id: newId(), text }],
      });
    } else if (role === "assistant") {
      const rawBlocks = Array.isArray(c.blocks) ? c.blocks : [];
      const blocks: MessageBlock[] = [];
      for (const b of rawBlocks as Array<Record<string, unknown>>) {
        if (b.type === "text" && typeof b.text === "string") {
          blocks.push({ kind: "text", id: newId(), text: b.text });
        } else if (b.type === "tool_use" && typeof b.id === "string") {
          const name = typeof b.name === "string" ? b.name : "tool";
          const input =
            b.input && typeof b.input === "object"
              ? (b.input as Record<string, unknown>)
              : undefined;
          blocks.push({
            kind: "tool",
            id: b.id,
            name,
            input,
          });
        }
      }
      out.push({ id: rawId, role: "assistant", seq, blocks });
    } else if (role === "tool_result") {
      const tuid = typeof c.tool_use_id === "string" ? c.tool_use_id : "";
      const result = typeof c.result === "string" ? c.result : "";
      // Attach result to the tool chip in the most recent assistant message
      for (let i = out.length - 1; i >= 0; i--) {
        const m = out[i];
        if (m.role !== "assistant") continue;
        const idx = m.blocks.findIndex(
          (b) => b.kind === "tool" && b.id === tuid,
        );
        if (idx >= 0) {
          const block = m.blocks[idx];
          if (block.kind === "tool") {
            const blocks = [...m.blocks];
            blocks[idx] = { ...block, result };
            out[i] = { ...m, blocks };
          }
          break;
        }
      }
    } else if (role === "dispatch_result") {
      const tuid = typeof c.tool_use_id === "string" ? c.tool_use_id : "";
      const runId = typeof c.run_id === "string" ? c.run_id : "";
      const callType = typeof c.call_type === "string" ? c.call_type : "";
      const headline = typeof c.headline === "string" ? c.headline : "";
      const status = c.status === "failed" ? "failed" : "completed";
      const summary = typeof c.summary === "string" ? c.summary : "";
      const traceUrl =
        typeof c.trace_url === "string" ? c.trace_url : `/traces/${runId}`;
      // Render as its own assistant-track dispatch chip, attached to the
      // assistant message that fired the tool_use if we can find it.
      const chip: MessageBlock = {
        kind: "dispatch",
        id: tuid || newId(),
        call_type: callType,
        headline,
        run_id: runId,
        status,
        summary,
        trace_url: traceUrl,
      };
      let placed = false;
      for (let i = out.length - 1; i >= 0; i--) {
        const m = out[i];
        if (m.role !== "assistant") continue;
        const hasTool = m.blocks.some(
          (b) => b.kind === "tool" && b.id === tuid,
        );
        if (hasTool) {
          out[i] = { ...m, blocks: [...m.blocks, chip] };
          placed = true;
          break;
        }
      }
      if (!placed) {
        out.push({
          id: rawId,
          role: "assistant",
          seq,
          blocks: [chip],
        });
      }
    }
  }
  return out;
}

export function ChatPanel() {
  const pathname = usePathname();
  const focusPageId = pathToQuestionId(pathname);

  const [open, setOpen] = useState(false);
  const [conversations, setConversations] = useState<ConversationListItem[]>([]);
  const [activeConvId, setActiveConvId] = useState<string | null>(null);
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [showConvList, setShowConvList] = useState(false);
  const [model, setModel] = useState<ModelShort>(DEFAULT_MODEL);

  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const setModelAndNote = useCallback((next: ModelShort) => {
    setModel(next);
    try {
      window.localStorage.setItem(MODEL_KEY, next);
    } catch {
      // ignore
    }
    const note: UiMessage = {
      id: newId(),
      role: "assistant",
      blocks: [
        {
          kind: "text",
          id: newId(),
          text: `— switched to ${MODEL_LABEL[next]} —`,
        },
      ],
    };
    setMessages((prev) => [...prev, note]);
  }, []);

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(OPEN_KEY);
      if (raw === "1") setOpen(true);
      const conv = window.localStorage.getItem(ACTIVE_CONV_KEY);
      if (conv) setActiveConvId(conv);
      const lastProject = window.localStorage.getItem(LAST_PROJECT_KEY);
      if (lastProject && UUID_RE.test(lastProject)) setProjectId(lastProject);
      const storedModel = window.localStorage.getItem(MODEL_KEY);
      if (storedModel && isModelShort(storedModel)) setModel(storedModel);
    } catch {
      // localStorage unavailable
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    (async () => {
      const fromPath = await resolveProjectFromPath(pathname);
      if (cancelled) return;
      if (fromPath) {
        setProjectId(fromPath);
        return;
      }
      if (projectId) return;
      const first = await fetchFirstProjectId();
      if (!cancelled && first) setProjectId(first);
    })();
    return () => {
      cancelled = true;
    };
  }, [open, pathname, projectId]);

  useEffect(() => {
    try {
      if (projectId && UUID_RE.test(projectId)) {
        window.localStorage.setItem(LAST_PROJECT_KEY, projectId);
      }
    } catch {
      // ignore
    }
  }, [projectId]);

  useEffect(() => {
    try {
      window.localStorage.setItem(OPEN_KEY, open ? "1" : "0");
    } catch {
      // ignore
    }
  }, [open]);

  useEffect(() => {
    try {
      if (activeConvId) {
        window.localStorage.setItem(ACTIVE_CONV_KEY, activeConvId);
      } else {
        window.localStorage.removeItem(ACTIVE_CONV_KEY);
      }
    } catch {
      // ignore
    }
  }, [activeConvId]);

  useEffect(() => {
    if (!open || !projectId) return;
    let cancelled = false;
    (async () => {
      try {
        const convs = await listConversations(projectId, { limit: 30 });
        if (cancelled) return;
        setConversations(convs);
      } catch (e) {
        console.warn("Failed to load conversations", e);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, projectId]);

  useEffect(() => {
    if (!activeConvId) {
      setMessages([]);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const detail = await getConversation(activeConvId);
        if (cancelled) return;
        const hydrated = hydrateFromDetail(detail.messages);
        setMessages(hydrated);
        if (detail.project_id && !projectId) {
          setProjectId(detail.project_id);
        }
      } catch (e) {
        console.warn("Failed to load conversation", e);
        setMessages([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeConvId, projectId]);

  useEffect(() => {
    if (!activeConvId) return;
    const unsub = subscribeConvEvents(activeConvId, (e: ConvEvent) => {
      if (e.event !== "dispatch_completed") return;
      const data = e.data;
      const chip: MessageBlock = {
        kind: "dispatch",
        id: data.tool_use_id || newId(),
        call_type: data.call_type,
        headline: data.headline,
        run_id: data.run_id,
        status: data.status,
        summary: data.summary,
        trace_url: data.trace_url,
      };
      setMessages((prev) => {
        for (let i = prev.length - 1; i >= 0; i--) {
          const m = prev[i];
          if (m.role !== "assistant") continue;
          const hasTool = m.blocks.some(
            (b) => b.kind === "tool" && b.id === data.tool_use_id,
          );
          if (hasTool) {
            const next = [...prev];
            next[i] = { ...m, blocks: [...m.blocks, chip] };
            return next;
          }
        }
        return [
          ...prev,
          {
            id: newId(),
            role: "assistant",
            blocks: [chip],
          },
        ];
      });
    });
    return unsub;
  }, [activeConvId]);

  useEffect(() => {
    if (!scrollRef.current) return;
    scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages, open]);

  const activeConvTitle = useMemo(() => {
    const c = conversations.find((x) => x.id === activeConvId);
    return c?.title || "new chat";
  }, [activeConvId, conversations]);

  const slashPrefix =
    input.startsWith("/") && !input.includes(" ")
      ? input.slice(1).toLowerCase()
      : null;
  const slashMatches: ModelShort[] =
    slashPrefix !== null ? MODELS.filter((m) => m.startsWith(slashPrefix)) : [];
  const showSlashDropdown = slashMatches.length > 0 && slashPrefix !== null;
  const [slashIndex, setSlashIndex] = useState(0);
  useEffect(() => {
    setSlashIndex(0);
  }, [slashPrefix]);

  const selectSlash = useCallback((name: ModelShort) => {
    setInput(`/${name}`);
    textareaRef.current?.focus();
  }, []);

  const handleSend = useCallback(async () => {
    const text = input.trim();
    if (!text || sending) return;

    if (text.startsWith("/")) {
      const token = text.slice(1).split(/\s+/)[0].toLowerCase();
      if (isModelShort(token)) {
        setInput("");
        setError(null);
        setModelAndNote(token);
        return;
      }
    }

    setInput("");
    setError(null);
    setSending(true);

    const userMsg: UiMessage = {
      id: newId(),
      role: "user",
      blocks: [{ kind: "text", id: newId(), text }],
    };
    setMessages((prev) => [...prev, userMsg]);

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      let convId = activeConvId;
      if (!convId) {
        let pid = projectId;
        if (!pid) {
          pid =
            (await resolveProjectFromPath(pathname)) ||
            (await fetchFirstProjectId());
          if (pid) setProjectId(pid);
        }
        if (!pid) {
          throw new Error(
            "no project available — open a project or page first",
          );
        }
        const created = await createConversation({
          project_id: pid,
          question_id: focusPageId,
          title: text.slice(0, 60),
        });
        convId = created.id;
        setProjectId(created.project_id);
        setActiveConvId(created.id);
        setConversations((prev) => [created, ...prev]);
      }

      const req = {
        question_id: focusPageId || "",
        messages: [{ role: "user", content: text }],
        conversation_id: convId,
        model,
        open_page_ids: focusPageId ? [focusPageId] : [],
      };

      setMessages((prev) => ensureInitialAssistant(prev));

      for await (const frame of streamChatTurn(req, controller.signal)) {
        handleStreamFrame(frame, setMessages, (cid) => {
          if (cid && cid !== convId) {
            convId = cid;
            setActiveConvId(cid);
          }
        });
      }

      setMessages((prev) => {
        const copy = [...prev];
        const last = copy[copy.length - 1];
        if (last && last.role === "assistant") {
          copy[copy.length - 1] = { ...last, pending: false };
        }
        return copy;
      });
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (!controller.signal.aborted) {
        setError(msg);
      }
    } finally {
      setSending(false);
    }
  }, [
    activeConvId,
    input,
    focusPageId,
    projectId,
    sending,
    model,
    pathname,
    setModelAndNote,
  ]);

  const handleNewConversation = useCallback(() => {
    abortRef.current?.abort();
    setActiveConvId(null);
    setMessages([]);
    setError(null);
    setShowConvList(false);
  }, []);

  const handlePickConversation = useCallback((id: string) => {
    abortRef.current?.abort();
    setActiveConvId(id);
    setShowConvList(false);
  }, []);

  const handleDeleteConversation = useCallback(
    async (id: string) => {
      try {
        await deleteConversation(id);
        setConversations((prev) => prev.filter((c) => c.id !== id));
        if (activeConvId === id) {
          setActiveConvId(null);
          setMessages([]);
        }
      } catch (e) {
        console.warn("delete failed", e);
      }
    },
    [activeConvId],
  );

  return (
    <>
      {!open && (
        <button
          type="button"
          className="chat-fab"
          onClick={() => setOpen(true)}
          aria-label="Open chat"
        >
          <span className="chat-fab-glyph">§</span>
          <span className="chat-fab-label">chat</span>
        </button>
      )}

      <aside
        className="chat-panel"
        data-open={open ? "1" : "0"}
        aria-hidden={!open}
      >
        <header className="chat-header">
          <button
            type="button"
            className="chat-title"
            onClick={() => setShowConvList((v) => !v)}
            aria-expanded={showConvList}
          >
            <span className="chat-title-rule">§</span>
            <span className="chat-title-text">{activeConvTitle}</span>
            <span className="chat-title-caret">{showConvList ? "▾" : "▸"}</span>
          </button>
          <div className="chat-header-actions">
            <button
              type="button"
              className="chat-icon-btn"
              onClick={handleNewConversation}
              title="New conversation"
            >
              +
            </button>
            <button
              type="button"
              className="chat-icon-btn"
              onClick={() => setOpen(false)}
              title="Close"
              aria-label="Close chat"
            >
              ×
            </button>
          </div>
        </header>

        {showConvList && (
          <div className="chat-conv-list">
            {conversations.length === 0 && (
              <div className="chat-conv-empty">
                no conversations yet — send a message to start
              </div>
            )}
            {conversations.map((c) => (
              <div
                key={c.id}
                className={
                  "chat-conv-row" + (c.id === activeConvId ? " active" : "")
                }
              >
                <button
                  type="button"
                  className="chat-conv-select"
                  onClick={() => handlePickConversation(c.id)}
                >
                  <span className="chat-conv-title">
                    {c.parent_conversation_id ? "↪ " : ""}
                    {c.title || "(untitled)"}
                  </span>
                  <span className="chat-conv-ts">
                    {new Date(c.updated_at).toLocaleDateString()}
                  </span>
                </button>
                <button
                  type="button"
                  className="chat-conv-del"
                  onClick={() => handleDeleteConversation(c.id)}
                  title="Delete"
                  aria-label="Delete conversation"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}

        <div className="chat-scope">
          {projectId ? (
            <span>
              project <code>{projectId.slice(0, 8)}</code>
              {focusPageId && (
                <>
                  {" · page "}
                  <code>{focusPageId.slice(0, 8)}</code>
                </>
              )}
            </span>
          ) : (
            <span className="chat-scope-warn">
              no project resolved — open a project, page, or trace
            </span>
          )}
          <button
            type="button"
            className="chat-model-btn"
            onClick={() => {
              const i = MODELS.indexOf(model);
              setModelAndNote(MODELS[(i + 1) % MODELS.length]);
            }}
            title="Click to cycle · or type /haiku /sonnet /opus"
          >
            model <code>{model}</code>
          </button>
        </div>

        <div className="chat-messages" ref={scrollRef}>
          {messages.length === 0 && (
            <div className="chat-empty">
              <p>
                ask about the workspace, search for pages, or dispatch a
                research call.
              </p>
              <ul>
                <li>“what do we know about …”</li>
                <li>“run find_considerations on this question”</li>
                <li>“list the root questions”</li>
              </ul>
            </div>
          )}
          {messages.map((m) => (
            <MessageView key={m.id} msg={m} />
          ))}
          {error && <div className="chat-error">{error}</div>}
        </div>

        <form
          className="chat-composer"
          onSubmit={(e) => {
            e.preventDefault();
            handleSend();
          }}
        >
          {showSlashDropdown && (
            <div className="chat-slash-dropdown" role="listbox">
              {slashMatches.map((name, i) => (
                <button
                  key={name}
                  type="button"
                  className={
                    "chat-slash-item" +
                    (i === slashIndex ? " active" : "") +
                    (name === model ? " current" : "")
                  }
                  onMouseEnter={() => setSlashIndex(i)}
                  onClick={() => selectSlash(name)}
                >
                  <span className="chat-slash-name">/{name}</span>
                  <span className="chat-slash-label">{MODEL_LABEL[name]}</span>
                  <span className="chat-slash-desc">{MODEL_DESC[name]}</span>
                  {name === model && (
                    <span className="chat-slash-current">current</span>
                  )}
                </button>
              ))}
              <div className="chat-slash-hint">tab to complete · esc to dismiss</div>
            </div>
          )}
          <textarea
            ref={textareaRef}
            className="chat-input"
            placeholder={sending ? "…" : "reply — try / for commands"}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            rows={2}
            onKeyDown={(e) => {
              if (showSlashDropdown) {
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setSlashIndex((i) => (i + 1) % slashMatches.length);
                  return;
                }
                if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setSlashIndex(
                    (i) =>
                      (i - 1 + slashMatches.length) % slashMatches.length,
                  );
                  return;
                }
                if (e.key === "Tab") {
                  e.preventDefault();
                  selectSlash(slashMatches[slashIndex]);
                  return;
                }
                if (e.key === "Escape") {
                  e.preventDefault();
                  setInput("");
                  return;
                }
              }
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                handleSend();
              }
            }}
            disabled={sending}
          />
          <button
            type="submit"
            className="chat-send"
            disabled={sending || !input.trim()}
            title="Send (⏎)"
          >
            ↵
          </button>
        </form>
      </aside>

      <style>{styles}</style>
    </>
  );
}

function handleStreamFrame(
  frame: StreamEvent,
  setMessages: React.Dispatch<React.SetStateAction<UiMessage[]>>,
  onConversationId: (id: string) => void,
) {
  if (frame.event === "conversation") {
    onConversationId(frame.data.conversation_id);
    return;
  }
  if (frame.event === "assistant_text_delta") {
    setMessages((prev) =>
      upsertTextDelta(ensureInitialAssistant(prev), frame.data.text),
    );
    return;
  }
  if (frame.event === "tool_use_start") {
    setMessages((prev) =>
      appendToolChip(
        ensureInitialAssistant(prev),
        frame.data.id,
        frame.data.name,
      ),
    );
    return;
  }
  if (frame.event === "tool_use_result") {
    setMessages((prev) => completeToolChip(prev, frame.data.id, frame.data.result));
    return;
  }
  if (frame.event === "error") {
    setMessages((prev) => {
      const copy = ensureInitialAssistant(prev);
      const last = copy[copy.length - 1];
      if (last && last.role === "assistant") {
        copy[copy.length - 1] = {
          ...last,
          blocks: [
            ...last.blocks,
            { kind: "text", id: newId(), text: `[error: ${frame.data.message}]` },
          ],
        };
      }
      return copy;
    });
  }
}

function MessageView({ msg }: { msg: UiMessage }) {
  if (msg.role === "user") {
    const text = msg.blocks.map((b) => (b.kind === "text" ? b.text : "")).join("");
    return (
      <div className="chat-msg chat-msg-user">
        <div className="chat-user-bubble">{text}</div>
      </div>
    );
  }
  const lastBlock = msg.blocks[msg.blocks.length - 1];
  const showThinking =
    msg.pending && (!lastBlock || lastBlock.kind !== "text");
  return (
    <div className="chat-msg chat-msg-assistant">
      {msg.blocks.map((block) => {
        if (block.kind === "text") {
          return (
            <p key={block.id} className="chat-text">
              {block.text}
              {msg.pending && block === lastBlock && (
                <span className="chat-cursor" aria-hidden>
                  ▍
                </span>
              )}
            </p>
          );
        }
        if (block.kind === "tool") {
          return <ToolChip key={block.id} block={block} />;
        }
        return <DispatchChip key={block.id} block={block} />;
      })}
      {showThinking && (
        <div
          className="chat-thinking"
          role="status"
          aria-label="assistant is thinking"
        >
          <span className="chat-thinking-dot" />
          <span className="chat-thinking-dot" />
          <span className="chat-thinking-dot" />
        </div>
      )}
    </div>
  );
}

function ToolChip({
  block,
}: {
  block: Extract<MessageBlock, { kind: "tool" }>;
}) {
  const [expanded, setExpanded] = useState(false);
  const preview = previewInput(block.input);
  const running = block.result === undefined;
  return (
    <div className={"chat-tool" + (running ? " running" : "")}>
      <button
        type="button"
        className="chat-tool-head"
        onClick={() => setExpanded((v) => !v)}
      >
        <span className="chat-tool-glyph">{running ? "◌" : "✓"}</span>
        <span className="chat-tool-name">{block.name}</span>
        {preview && <span className="chat-tool-preview">{preview}</span>}
        <span className="chat-tool-caret">{expanded ? "−" : "+"}</span>
      </button>
      {expanded && (
        <div className="chat-tool-body">
          {block.input && (
            <>
              <div className="chat-tool-label">input</div>
              <pre className="chat-tool-pre">
                {JSON.stringify(block.input, null, 2)}
              </pre>
            </>
          )}
          <div className="chat-tool-label">
            result {running && <em>(pending…)</em>}
          </div>
          <pre className="chat-tool-pre">{block.result ?? ""}</pre>
        </div>
      )}
    </div>
  );
}

function DispatchChip({
  block,
}: {
  block: Extract<MessageBlock, { kind: "dispatch" }>;
}) {
  const ok = block.status === "completed";
  return (
    <div className={"chat-dispatch" + (ok ? " ok" : " fail")}>
      <span className="chat-dispatch-glyph">{ok ? "◆" : "⚠"}</span>
      <span className="chat-dispatch-body">
        <span className="chat-dispatch-type">{block.call_type}</span>
        <span className="chat-dispatch-headline">
          {block.headline.length > 50
            ? block.headline.slice(0, 48) + "…"
            : block.headline}
        </span>
        <span className="chat-dispatch-summary">{block.summary}</span>
      </span>
      <Link
        href={block.trace_url}
        className="chat-dispatch-trace"
        title="Open trace"
      >
        trace →
      </Link>
    </div>
  );
}

const styles = `
.chat-fab {
  position: fixed;
  right: 1.25rem;
  bottom: 1.25rem;
  z-index: 50;
  display: inline-flex;
  align-items: baseline;
  gap: 0.4rem;
  padding: 0.45rem 0.85rem;
  background: var(--background);
  border: 1px solid var(--color-border);
  border-radius: 999px;
  font-family: var(--font-geist-sans), system-ui, sans-serif;
  font-size: 0.78rem;
  letter-spacing: 0.04em;
  color: var(--color-accent);
  cursor: pointer;
  box-shadow: 0 6px 20px -10px rgba(0, 0, 0, 0.18);
  transition: border-color 120ms ease, transform 120ms ease;
}
.chat-fab:hover {
  border-color: var(--color-accent);
  transform: translateY(-1px);
}
.chat-fab-glyph {
  font-family: var(--font-geist-mono), ui-monospace, monospace;
  font-size: 0.95rem;
  color: var(--color-muted);
  position: relative;
  top: 0.05em;
}
.chat-fab-label {
  text-transform: lowercase;
}

.chat-panel {
  position: fixed;
  top: 0;
  right: 0;
  width: min(420px, 100vw);
  height: 100vh;
  z-index: 40;
  background: var(--background);
  border-left: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  font-family: var(--font-geist-sans), system-ui, sans-serif;
  font-size: 0.875rem;
  color: var(--foreground);
  transform: translateX(100%);
  transition: transform 220ms cubic-bezier(0.32, 0.72, 0.32, 1);
  box-shadow: -16px 0 40px -28px rgba(0, 0, 0, 0.25);
}
.chat-panel[data-open="1"] {
  transform: translateX(0);
}

.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.6rem 0.75rem 0.55rem 0.85rem;
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface);
}
.chat-title {
  display: inline-flex;
  align-items: baseline;
  gap: 0.45rem;
  background: none;
  border: none;
  padding: 0.15rem 0.25rem;
  margin: 0;
  color: var(--foreground);
  font-family: inherit;
  font-size: 0.88rem;
  cursor: pointer;
  max-width: 18rem;
  overflow: hidden;
}
.chat-title:hover {
  color: var(--color-accent);
}
.chat-title-rule {
  font-family: var(--font-geist-mono), ui-monospace, monospace;
  color: var(--color-dim);
  font-size: 0.85em;
}
.chat-title-text {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.chat-title-caret {
  color: var(--color-dim);
  font-size: 0.75em;
}
.chat-header-actions {
  display: inline-flex;
  gap: 0.15rem;
}
.chat-icon-btn {
  background: none;
  border: 1px solid transparent;
  padding: 0.1rem 0.45rem;
  font-family: var(--font-geist-mono), ui-monospace, monospace;
  font-size: 0.95rem;
  color: var(--color-muted);
  cursor: pointer;
  border-radius: 4px;
}
.chat-icon-btn:hover {
  border-color: var(--color-border);
  color: var(--foreground);
}

.chat-conv-list {
  max-height: 16rem;
  overflow-y: auto;
  border-bottom: 1px solid var(--color-border);
  background: var(--background);
}
.chat-conv-empty {
  padding: 0.9rem 1rem;
  color: var(--color-muted);
  font-size: 0.78rem;
  font-style: italic;
}
.chat-conv-row {
  display: flex;
  align-items: stretch;
  border-bottom: 1px dotted var(--color-border);
}
.chat-conv-row:last-child {
  border-bottom: none;
}
.chat-conv-row.active .chat-conv-title {
  color: var(--foreground);
  font-weight: 500;
}
.chat-conv-select {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  background: none;
  border: none;
  text-align: left;
  padding: 0.55rem 0.75rem;
  cursor: pointer;
  font-family: inherit;
  color: var(--color-accent);
  overflow: hidden;
}
.chat-conv-select:hover {
  background: var(--color-surface);
}
.chat-conv-title {
  font-size: 0.82rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.chat-conv-ts {
  font-family: var(--font-geist-mono), ui-monospace, monospace;
  font-size: 0.68rem;
  color: var(--color-dim);
}
.chat-conv-del {
  background: none;
  border: none;
  padding: 0 0.6rem;
  color: var(--color-dim);
  font-size: 1rem;
  cursor: pointer;
}
.chat-conv-del:hover {
  color: var(--type-judgement);
}

.chat-scope {
  padding: 0.4rem 0.9rem;
  border-bottom: 1px dotted var(--color-border);
  font-family: var(--font-geist-mono), ui-monospace, monospace;
  font-size: 0.7rem;
  color: var(--color-muted);
  background: var(--background);
}
.chat-scope code {
  color: var(--color-accent);
}
.chat-scope-warn {
  color: var(--type-judgement);
}
.chat-scope {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}
.chat-model-btn {
  background: none;
  border: 1px dotted var(--color-border);
  border-radius: 3px;
  padding: 0.12rem 0.4rem;
  font-family: var(--font-geist-mono), ui-monospace, monospace;
  font-size: 0.68rem;
  color: var(--color-muted);
  cursor: pointer;
  letter-spacing: 0.04em;
}
.chat-model-btn:hover {
  border-style: solid;
  border-color: var(--color-accent);
  color: var(--foreground);
}
.chat-model-btn code {
  color: var(--type-concept);
  margin-left: 0.2rem;
}

.chat-composer {
  position: relative;
}
.chat-slash-dropdown {
  position: absolute;
  left: 0.75rem;
  right: 0.75rem;
  bottom: calc(100% - 0.1rem);
  margin-bottom: 0.35rem;
  background: var(--background);
  border: 1px solid var(--color-border);
  border-radius: 3px;
  box-shadow: 0 -8px 24px -18px rgba(0, 0, 0, 0.25);
  overflow: hidden;
  z-index: 2;
}
.chat-slash-item {
  display: grid;
  grid-template-columns: auto 1fr auto;
  column-gap: 0.5rem;
  align-items: baseline;
  width: 100%;
  background: none;
  border: none;
  border-bottom: 1px dotted var(--color-border);
  padding: 0.4rem 0.6rem;
  text-align: left;
  font-family: inherit;
  color: var(--color-accent);
  cursor: pointer;
}
.chat-slash-item:last-of-type {
  border-bottom: none;
}
.chat-slash-item.active {
  background: var(--color-surface);
  color: var(--foreground);
}
.chat-slash-item.current .chat-slash-name {
  color: var(--type-concept);
}
.chat-slash-name {
  font-family: var(--font-geist-mono), ui-monospace, monospace;
  font-size: 0.78rem;
  color: var(--foreground);
}
.chat-slash-label {
  font-size: 0.76rem;
  color: var(--color-accent);
}
.chat-slash-desc {
  grid-column: 2 / 3;
  font-size: 0.68rem;
  color: var(--color-muted);
}
.chat-slash-current {
  font-family: var(--font-geist-mono), ui-monospace, monospace;
  font-size: 0.66rem;
  color: var(--type-concept);
  align-self: center;
}
.chat-slash-hint {
  padding: 0.25rem 0.6rem;
  font-family: var(--font-geist-mono), ui-monospace, monospace;
  font-size: 0.64rem;
  color: var(--color-dim);
  background: var(--color-surface);
  border-top: 1px dotted var(--color-border);
  letter-spacing: 0.04em;
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 1rem 1rem 1.2rem;
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
}

.chat-empty {
  color: var(--color-muted);
  font-size: 0.82rem;
  line-height: 1.5;
  padding: 0.25rem 0.1rem;
}
.chat-empty ul {
  margin: 0.6rem 0 0;
  padding-left: 1rem;
  list-style: "· ";
  color: var(--color-dim);
}
.chat-empty li {
  margin-bottom: 0.2rem;
}

.chat-msg-user {
  display: flex;
  justify-content: flex-end;
}
.chat-user-bubble {
  max-width: 85%;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 2px 10px 10px 10px;
  padding: 0.5rem 0.7rem;
  font-size: 0.88rem;
  white-space: pre-wrap;
  line-height: 1.45;
}

.chat-msg-assistant {
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
}
.chat-text {
  margin: 0;
  font-size: 0.88rem;
  line-height: 1.55;
  color: var(--foreground);
  white-space: pre-wrap;
}
.chat-cursor {
  display: inline-block;
  animation: chat-blink 1.1s steps(2) infinite;
  color: var(--color-muted);
  margin-left: 1px;
}
@keyframes chat-blink {
  50% {
    opacity: 0;
  }
}

.chat-thinking {
  display: inline-flex;
  gap: 0.25rem;
  padding: 0.2rem 0.1rem;
  align-items: center;
}
.chat-thinking-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--color-muted);
  opacity: 0.35;
  animation: chat-thinking-bounce 1.3s ease-in-out infinite;
}
.chat-thinking-dot:nth-child(2) {
  animation-delay: 0.18s;
}
.chat-thinking-dot:nth-child(3) {
  animation-delay: 0.36s;
}
@keyframes chat-thinking-bounce {
  0%, 70%, 100% {
    opacity: 0.25;
    transform: translateY(0);
  }
  30% {
    opacity: 0.9;
    transform: translateY(-2px);
  }
}

.chat-tool {
  border: 1px dashed var(--color-border);
  border-radius: 3px;
  background: var(--color-surface);
  font-family: var(--font-geist-mono), ui-monospace, monospace;
}
.chat-tool.running {
  border-style: dotted;
}
.chat-tool-head {
  display: flex;
  align-items: baseline;
  gap: 0.45rem;
  width: 100%;
  background: none;
  border: none;
  padding: 0.35rem 0.55rem;
  color: var(--color-accent);
  font-family: inherit;
  font-size: 0.76rem;
  cursor: pointer;
  text-align: left;
}
.chat-tool-head:hover {
  background: var(--color-surface);
}
.chat-tool-glyph {
  color: var(--type-concept);
  font-size: 0.8rem;
}
.chat-tool.running .chat-tool-glyph {
  color: var(--type-judgement);
  animation: chat-spin 1.6s linear infinite;
}
@keyframes chat-spin {
  100% {
    transform: rotate(360deg);
  }
}
.chat-tool-name {
  color: var(--foreground);
}
.chat-tool-preview {
  color: var(--color-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}
.chat-tool-caret {
  color: var(--color-dim);
  font-family: inherit;
}
.chat-tool-body {
  border-top: 1px dashed var(--color-border);
  padding: 0.5rem 0.6rem 0.6rem;
}
.chat-tool-label {
  color: var(--color-muted);
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-bottom: 0.2rem;
}
.chat-tool-label em {
  color: var(--color-dim);
  font-style: italic;
  text-transform: none;
  letter-spacing: 0;
}
.chat-tool-pre {
  margin: 0 0 0.5rem;
  font-size: 0.72rem;
  color: var(--foreground);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 18rem;
  overflow-y: auto;
  line-height: 1.4;
}
.chat-tool-pre:last-child {
  margin-bottom: 0;
}

.chat-dispatch {
  display: flex;
  align-items: flex-start;
  gap: 0.55rem;
  padding: 0.5rem 0.6rem;
  border: 1px solid var(--type-question-border);
  background: var(--type-question-bg);
  border-radius: 3px;
  font-size: 0.78rem;
}
.chat-dispatch.fail {
  border-color: var(--type-judgement-border);
  background: var(--type-judgement-bg);
}
.chat-dispatch-glyph {
  color: var(--type-question);
  font-size: 0.85rem;
  position: relative;
  top: 0.1em;
}
.chat-dispatch.fail .chat-dispatch-glyph {
  color: var(--type-judgement);
}
.chat-dispatch-body {
  display: flex;
  flex-direction: column;
  flex: 1;
  gap: 0.15rem;
  min-width: 0;
}
.chat-dispatch-type {
  font-family: var(--font-geist-mono), ui-monospace, monospace;
  font-size: 0.7rem;
  color: var(--color-accent);
  letter-spacing: 0.04em;
}
.chat-dispatch-headline {
  color: var(--foreground);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chat-dispatch-summary {
  color: var(--color-muted);
  font-size: 0.74rem;
  line-height: 1.4;
}
.chat-dispatch-trace {
  font-family: var(--font-geist-mono), ui-monospace, monospace;
  font-size: 0.72rem;
  color: var(--type-claim);
  text-decoration: none;
  white-space: nowrap;
  align-self: flex-start;
}
.chat-dispatch-trace:hover {
  text-decoration: underline;
}

.chat-error {
  border: 1px solid var(--type-judgement-border);
  background: var(--type-judgement-bg);
  color: var(--type-judgement);
  padding: 0.45rem 0.6rem;
  font-size: 0.78rem;
  border-radius: 3px;
}

.chat-composer {
  display: flex;
  align-items: flex-end;
  gap: 0.4rem;
  padding: 0.6rem 0.75rem 0.8rem;
  border-top: 1px solid var(--color-border);
  background: var(--color-surface);
}
.chat-input {
  flex: 1;
  resize: none;
  min-height: 2.4rem;
  max-height: 12rem;
  padding: 0.45rem 0.55rem;
  font-family: inherit;
  font-size: 0.88rem;
  color: var(--foreground);
  background: var(--background);
  border: 1px solid var(--color-border);
  border-radius: 3px;
  line-height: 1.4;
}
.chat-input:focus {
  outline: none;
  border-color: var(--color-accent);
}
.chat-input:disabled {
  color: var(--color-muted);
}
.chat-send {
  background: none;
  border: 1px solid var(--color-border);
  border-radius: 3px;
  padding: 0.45rem 0.65rem;
  font-family: var(--font-geist-mono), ui-monospace, monospace;
  font-size: 1rem;
  color: var(--color-accent);
  cursor: pointer;
  line-height: 1;
}
.chat-send:hover:not(:disabled) {
  border-color: var(--color-accent);
  color: var(--foreground);
}
.chat-send:disabled {
  color: var(--color-dim);
  cursor: not-allowed;
}

@media (max-width: 700px) {
  .chat-panel {
    width: 100vw;
  }
}
`;

export default ChatPanel;
