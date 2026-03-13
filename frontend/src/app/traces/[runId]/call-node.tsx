"use client";

import { useState } from "react";
import Link from "next/link";
import type {
  Call,
  CallTraceOut,
  DispatchExecutedEventOut,
  DispatchesPlannedEventOut,
  LlmExchangeOut,
  PageRef,
} from "@/api/types.gen";

type TraceEvent = CallTraceOut["events"][number];

const CALL_TYPE_ACCENT: Record<string, string> = {
  scout: "#5b8def",
  assess: "#a07cdf",
  prioritization: "#d4943a",
  ingest: "#4dab6f",
  reframe: "#c46b6b",
  maintain: "#7a8a9e",
};

function compactTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${(n / 1_000).toFixed(0)}k`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function TokenMeter({
  inputTokens,
  outputTokens,
  cacheRead,
  cacheCreate,
  costUsd,
}: {
  inputTokens: number;
  outputTokens: number | null | undefined;
  cacheRead: number | null | undefined;
  cacheCreate: number | null | undefined;
  costUsd: number | null | undefined;
}) {
  const cr = cacheRead || 0;
  const cc = cacheCreate || 0;
  const total = inputTokens + cr + cc;
  const hasCache = cr > 0 || cc > 0;
  const readPct = total > 0 ? (cr / total) * 100 : 0;
  const createPct = total > 0 ? (cc / total) * 100 : 0;
  const cacheHitPct = total > 0 ? Math.round((cr / total) * 100) : 0;

  return (
    <span className="trace-token-meter">
      {hasCache && (
        <span className="trace-token-bar-wrap" title={
          `Cache read: ${cr.toLocaleString()} · `
          + `Cache write: ${cc.toLocaleString()} · `
          + `Fresh: ${inputTokens.toLocaleString()}`
        }>
          <span className="trace-token-bar">
            <span
              className="trace-token-bar-read"
              style={{ width: `${readPct}%` }}
            />
            <span
              className="trace-token-bar-create"
              style={{ width: `${createPct}%` }}
            />
          </span>
          {cacheHitPct > 0 && (
            <span className="trace-cache-pct">{cacheHitPct}%</span>
          )}
        </span>
      )}
      <span className="trace-token-compact">
        {compactTokens(inputTokens + cr + cc)}{"\u2009\u2192\u2009"}{outputTokens != null ? compactTokens(outputTokens) : "?"}
      </span>
      {costUsd != null && (
        <span className="trace-cost">${costUsd.toFixed(4)}</span>
      )}
    </span>
  );
}

function formatTime(ts: string): string {
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return ts;
  }
}

function getDuration(call: Call): string | null {
  if (!call.created_at || !call.completed_at) return null;
  const start = new Date(call.created_at).getTime();
  const end = new Date(call.completed_at).getTime();
  const secs = Math.round((end - start) / 1000);
  if (secs < 60) return `${secs}s`;
  return `${Math.floor(secs / 60)}m${secs % 60}s`;
}

function PageChip({ page }: { page: PageRef }) {
  const short = page.id.slice(0, 8);
  const label = page.summary || short;
  return (
    <Link href={`/pages/${page.id}`} className="trace-page-chip" title={short}>
      {label}
    </Link>
  );
}

function PageList({ pages }: { pages: PageRef[] }) {
  if (!pages || pages.length === 0)
    return <span className="trace-empty">none</span>;
  return (
    <span className="trace-page-list">
      {pages.map((p, i) => (
        <PageChip key={p.id} page={p} />
      ))}
    </span>
  );
}

function RoleBadge({ role }: { role: string }) {
  const cls = role === "direct" ? "trace-role-badge-direct" : "trace-role-badge-structural";
  return <span className={`trace-role-badge ${cls}`}>{role}</span>;
}

function MoveRow({
  moveType,
  summary,
  pageRefs,
  extra,
}: {
  moveType: string;
  summary: string;
  pageRefs?: PageRef[];
  extra?: Record<string, unknown>;
}) {
  const isCreate = moveType.startsWith("CREATE_");
  const isLink = moveType.startsWith("LINK_");
  const isSupersede = moveType === "SUPERSEDE_PAGE";
  const isHypothesis = moveType === "PROPOSE_HYPOTHESIS";
  const isLoad = moveType === "LOAD_PAGE";
  const isChange = moveType === "CHANGE_LINK_ROLE";
  const isRemove = moveType === "REMOVE_LINK";

  const typeClass = isCreate
    ? "trace-move-create"
    : isLink
      ? "trace-move-link"
      : isSupersede
        ? "trace-move-supersede"
        : isHypothesis
          ? "trace-move-hypothesis"
          : isLoad
            ? "trace-move-load"
            : isChange
              ? "trace-move-change"
              : isRemove
                ? "trace-move-remove"
                : "trace-move-default";

  const hasRefs = pageRefs && pageRefs.length > 0;

  if (isChange && extra?.old_role && extra?.new_role) {
    const fromPage = extra.from_page as { id: string; summary: string } | undefined;
    const toPage = extra.to_page as { id: string; summary: string } | undefined;
    return (
      <div className="trace-move-row">
        <span className={`trace-move-type ${typeClass}`}>
          change role
        </span>
        <span className="trace-role-change-detail">
          {fromPage && <PageChip page={fromPage} />}
          {toPage && (
            <>
              <span className="trace-role-arrow">{"\u2192"}</span>
              <PageChip page={toPage} />
            </>
          )}
          <RoleBadge role={String(extra.old_role)} />
          <span className="trace-role-arrow">{"\u2192"}</span>
          <RoleBadge role={String(extra.new_role)} />
        </span>
      </div>
    );
  }

  if (isRemove && extra?.from_page) {
    const fromPage = extra.from_page as { id: string; summary: string } | undefined;
    const toPage = extra.to_page as { id: string; summary: string } | undefined;
    const role = extra.role as string | undefined;
    return (
      <div className="trace-move-row">
        <span className={`trace-move-type ${typeClass}`}>
          remove link
        </span>
        <span className="trace-role-change-detail">
          {fromPage && <PageChip page={fromPage} />}
          {toPage && (
            <>
              <span className="trace-role-arrow">{"\u2192"}</span>
              <PageChip page={toPage} />
            </>
          )}
          {role && <RoleBadge role={role} />}
        </span>
      </div>
    );
  }

  return (
    <div className="trace-move-row">
      <span className={`trace-move-type ${typeClass}`}>
        {moveType.replace(/_/g, " ").toLowerCase()}
      </span>
      {!hasRefs && summary && (
        <span className="trace-move-summary">{summary}</span>
      )}
      {hasRefs && (
        <span className="trace-page-list">
          {pageRefs.map((p) => (
            <PageChip key={p.id} page={p} />
          ))}
        </span>
      )}
    </div>
  );
}

const API_BASE =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

function CollapsiblePre({
  label,
  content,
}: {
  label: string;
  content: string | null;
}) {
  const [open, setOpen] = useState(false);
  if (!content) return null;

  return (
    <div className="trace-collapsible">
      <button
        onClick={() => setOpen(!open)}
        className="trace-collapsible-toggle"
      >
        <span className="trace-collapsible-icon">{open ? "\u2013" : "+"}</span>
        <span>{label}</span>
        <span className="trace-collapsible-meta">
          {content.length.toLocaleString()} chars
        </span>
      </button>
      {open && (
        <pre className="trace-collapsible-content">{content}</pre>
      )}
      {!open && content.length > 200 && (
        <pre className="trace-collapsible-preview">
          {content.slice(0, 200)}...
        </pre>
      )}
    </div>
  );
}

function formatBlockContent(block: Record<string, unknown>): string {
  if (block.type === "text") return String(block.text ?? "");
  if (block.type === "tool_use")
    return `[tool_use: ${block.name}]\n${JSON.stringify(block.input, null, 2)}`;
  if (block.type === "tool_result") {
    const content = block.content;
    const text = typeof content === "string" ? content : JSON.stringify(content);
    return `[tool_result: ${block.tool_use_id}]\n${text}`;
  }
  return JSON.stringify(block, null, 2);
}

function formatMessageContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content))
    return content.map((b) => formatBlockContent(b as Record<string, unknown>)).join("\n");
  return String(content ?? "");
}

function MessageThread({ messages }: { messages: Array<Record<string, unknown>> }) {
  return (
    <div className="trace-message-thread">
      {messages.map((msg, i) => {
        const role = String(msg.role ?? "unknown");
        const text = formatMessageContent(msg.content);
        return (
          <details key={i} className="trace-message-turn">
            <summary className={`trace-message-role trace-role-${role}`}>
              {role}
              <span className="trace-collapsible-meta">
                {" "}{text.length.toLocaleString()} chars
              </span>
            </summary>
            <pre className="trace-collapsible-content">{text}</pre>
          </details>
        );
      })}
    </div>
  );
}

function ExchangeDetail({ detail }: { detail: LlmExchangeOut }) {
  return (
    <div className="trace-exchange-detail">
      <CollapsiblePre label="System prompt" content={detail.system_prompt} />
      {detail.user_messages && detail.user_messages.length > 0 ? (
        <MessageThread messages={detail.user_messages as Array<Record<string, unknown>>} />
      ) : (
        <CollapsiblePre label="User message" content={detail.user_message} />
      )}
      <CollapsiblePre label="Response" content={detail.response_text} />
      {detail.tool_calls.length > 0 && (
        <div className="trace-tool-calls">
          <div className="trace-tool-calls-label">
            Tool calls ({detail.tool_calls.length})
          </div>
          {detail.tool_calls.map((tc, i) => (
            <details key={i} className="trace-tool-call">
              <summary className="trace-tool-call-name">
                {tc.name as string}
              </summary>
              <pre className="trace-tool-call-input">
                {JSON.stringify(tc.input, null, 2)}
              </pre>
              {tc.result ? (
                <pre className="trace-tool-call-output">
                  {String(tc.result)}
                </pre>
              ) : null}
            </details>
          ))}
        </div>
      )}
      {detail.error && (
        <div className="trace-exchange-error-detail">
          {detail.error}
        </div>
      )}
    </div>
  );
}

function StatusDot({ status }: { status: string }) {
  const colorClass =
    status === "running"
      ? "trace-dot-running"
      : status === "complete"
        ? "trace-dot-complete"
        : status === "failed"
          ? "trace-dot-failed"
          : "trace-dot-pending";

  return <span className={`trace-status-dot ${colorClass}`} />;
}

function EventSection({ event }: { event: TraceEvent }) {
  const isWarning = event.event === "warning";
  const isError = event.event === "error";
  const isExchange = event.event === "llm_exchange";

  const [exchangeDetail, setExchangeDetail] = useState<LlmExchangeOut | null>(null);
  const [exchangeOpen, setExchangeOpen] = useState(false);
  const [exchangeLoading, setExchangeLoading] = useState(false);

  async function toggleExchange() {
    if (!isExchange) return;
    if (exchangeDetail) {
      setExchangeOpen(!exchangeOpen);
      return;
    }
    setExchangeLoading(true);
    try {
      const res = await fetch(
        `${API_BASE}/api/llm-exchanges/${event.exchange_id}`,
      );
      if (res.ok) {
        setExchangeDetail(await res.json());
        setExchangeOpen(true);
      }
    } finally {
      setExchangeLoading(false);
    }
  }

  return (
    <div
      className={`trace-event ${isWarning ? "trace-event-warning" : ""} ${isError ? "trace-event-error" : ""}`}
    >
      <div
        className={`trace-event-header ${isExchange ? "trace-event-header-clickable" : ""}`}
        onClick={isExchange ? toggleExchange : undefined}
        role={isExchange ? "button" : undefined}
      >
        <span className="trace-event-label">
          {isExchange ? (exchangeOpen ? "\u2013" : "+") + " " : ""}
          {event.event.replace(/_/g, " ")}
        </span>
        {isExchange && (
          <span className="trace-exchange-info">
            {event.phase.replace(/_/g, " ")}{event.round != null ? ` round ${event.round}` : ""}
            {event.input_tokens != null && (
              <TokenMeter
                inputTokens={event.input_tokens}
                outputTokens={event.output_tokens}
                cacheRead={event.cache_read_input_tokens}
                cacheCreate={event.cache_creation_input_tokens}
                costUsd={event.cost_usd}
              />
            )}
            {event.duration_ms != null && (
              <span className="trace-duration">
                {(event.duration_ms / 1000).toFixed(1)}s
              </span>
            )}
            {exchangeLoading && (
              <span className="trace-exchange-loading">loading...</span>
            )}
          </span>
        )}
        <span className="trace-event-time">{formatTime(event.ts)}</span>
      </div>

      {isExchange && exchangeOpen && exchangeDetail && (
        <ExchangeDetail detail={exchangeDetail} />
      )}

      {event.event === "context_built" && (
        <div className="trace-event-body">
          {(event.working_context_page_ids ?? []).length > 0 && (
            <div className="trace-kv">
              <span className="trace-kv-key">working context</span>
              <PageList pages={event.working_context_page_ids ?? []} />
            </div>
          )}
          {(event.preloaded_page_ids ?? []).length > 0 && (
            <div className="trace-kv">
              <span className="trace-kv-key">preloaded</span>
              <PageList pages={event.preloaded_page_ids ?? []} />
            </div>
          )}
          {event.budget != null && (
            <div className="trace-kv">
              <span className="trace-kv-key">budget</span>
              <span className="trace-kv-value">{event.budget}</span>
            </div>
          )}
        </div>
      )}

      {event.event === "moves_executed" && (
        <div className="trace-event-body">
          {(event.moves ?? []).map((m, i) => (
            <MoveRow
              key={i}
              moveType={m.type}
              summary={m.summary || ""}
              pageRefs={m.page_refs}
              extra={m as unknown as Record<string, unknown>}
            />
          ))}
        </div>
      )}

      {event.event === "review_complete" && (
        <div className="trace-event-body">
          <div className="trace-review-metrics">
            <span className="trace-kv-key">remaining fruit</span>
            <span className="trace-kv-value">{String(event.remaining_fruit)}</span>
            <span className="trace-kv-key">confidence</span>
            <span className="trace-kv-value">{String(event.confidence)}</span>
          </div>
        </div>
      )}

      {event.event === "warning" && (
        <div className="trace-event-body trace-warning-text">
          {event.message}
        </div>
      )}

      {event.event === "error" && (
        <div className="trace-event-body trace-error-text">
          {event.message}
        </div>
      )}

      {event.event === "dispatches_planned" && (
        <div className="trace-event-body">
          {(event.dispatches ?? []).map((d, i) => (
            <div key={i} className="trace-dispatch-row">
              <span className="trace-dispatch-index">{i + 1}</span>
              <span
                className="trace-dispatch-type"
                style={{ color: CALL_TYPE_ACCENT[d.call_type] || "#7a8a9e" }}
              >
                {d.call_type}
              </span>
              {d.reason ? (
                <span className="trace-dispatch-reason">{String(d.reason)}</span>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function CallNode({
  trace,
  depth,
}: {
  trace: CallTraceOut;
  depth: number;
}) {
  const [isOpen, setIsOpen] = useState(depth <= 1);
  const { call, events, children } = trace;
  const shortId = call.id.slice(0, 8);
  const duration = getDuration(call);
  const accent = CALL_TYPE_ACCENT[call.call_type] || "#7a8a9e";

  const warningCount = events.filter((e) => e.event === "warning").length;
  const errorCount = events.filter((e) => e.event === "error").length;

  const skipEvents = new Set([
    "dispatches_planned", "dispatch_executed",
  ]);
  const displayableEvents = events.filter((e) => !skipEvents.has(e.event));

  const dispatchEvents = events.filter(
    (e): e is DispatchesPlannedEventOut => e.event === "dispatches_planned",
  );
  const executedMap = new Map<number, DispatchExecutedEventOut>();
  for (const e of events) {
    if (e.event === "dispatch_executed") {
      executedMap.set(e.index, e);
    }
  }

  return (
    <div
      id={`call-${shortId}`}
      className="trace-call-node"
      style={
        {
          "--call-accent": accent,
          marginLeft: depth > 0 ? "20px" : "0",
          borderLeft: `3px solid ${accent}`,
          paddingLeft: "10px",
        } as React.CSSProperties
      }
    >
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="trace-call-header"
      >
        <span className="trace-call-type">
          {call.call_type}
        </span>
        {trace.scope_page_summary && (
          <span className="trace-call-scope">{trace.scope_page_summary}</span>
        )}
        <span className="trace-call-id">{shortId}</span>
        <span className="trace-call-meta">
          <StatusDot status={call.status} />
          <span className="trace-call-status">{call.status}</span>
          {duration && <span className="trace-call-duration">{duration}</span>}
          {trace.cost_usd != null && (
            <span className="trace-call-cost">${trace.cost_usd.toFixed(4)}</span>
          )}
        </span>
        {warningCount > 0 && (
          <span className="trace-badge-warning">
            {warningCount} warn
          </span>
        )}
        {errorCount > 0 && (
          <span className="trace-badge-error">
            {errorCount} err
          </span>
        )}
        <span className="trace-call-chevron">{isOpen ? "\u2013" : "+"}</span>
      </button>

      {isOpen && (
        <div className="trace-call-body">
          {call.call_params && Object.keys(call.call_params).length > 0 && (
            <div className="trace-call-params">
              {Object.entries(call.call_params).map(([key, value]) => (
                <span key={key} className="trace-call-param">
                  <span className="trace-call-param-label">
                    {key.replace(/_/g, " ")}
                  </span>
                  <span className="trace-call-param-value">{String(value)}</span>
                </span>
              ))}
            </div>
          )}

          {dispatchEvents.length > 0 && (
            <div className="trace-dispatches">
              <div className="trace-section-label">dispatches</div>
              <div className="trace-dispatch-list">
                {(dispatchEvents[0]?.dispatches ?? []).map((d, i) => {
                  const ex = executedMap.get(i);
                  const childCallId = ex?.child_call_id;
                  return (
                    <div key={i} className="trace-dispatch-item">
                      <span className="trace-dispatch-index">{i + 1}</span>
                      {childCallId ? (
                        <a
                          href={`#call-${childCallId.slice(0, 8)}`}
                          className="trace-dispatch-link"
                          style={{ color: CALL_TYPE_ACCENT[d.call_type] || "#7a8a9e" }}
                          onClick={(e) => {
                            e.preventDefault();
                            document
                              .getElementById(
                                `call-${childCallId.slice(0, 8)}`,
                              )
                              ?.scrollIntoView({ behavior: "smooth" });
                          }}
                        >
                          {d.call_type}
                        </a>
                      ) : (
                        <span
                          className="trace-dispatch-skipped"
                          style={{ color: CALL_TYPE_ACCENT[d.call_type] || "#7a8a9e" }}
                        >
                          {d.call_type}
                        </span>
                      )}
                      {d.reason ? (
                        <span className="trace-dispatch-reason">
                          {String(d.reason)}
                        </span>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {displayableEvents.length > 0 && (
            <div className="trace-events">
              {displayableEvents.map((ev, i) => (
                <EventSection key={`${ev.ts}-${i}`} event={ev} />
              ))}
            </div>
          )}

          {children.length > 0 && (
            <div className="trace-children">
              {children.map((child) => (
                <CallNode key={child.call.id} trace={child} depth={depth + 1} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
