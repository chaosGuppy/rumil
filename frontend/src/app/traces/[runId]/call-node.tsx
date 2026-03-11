"use client";

import { useState } from "react";
import Link from "next/link";
import type {
  Call,
  CallTraceOut,
  DispatchExecutedEventOut,
  DispatchesPlannedEventOut,
} from "@/api/types.gen";
import { LLMExchangeDetail } from "./llm-exchange-detail";

type TraceEvent = CallTraceOut["events"][number];

const CALL_TYPE_ACCENT: Record<string, string> = {
  scout: "#5b8def",
  assess: "#a07cdf",
  prioritization: "#d4943a",
  ingest: "#4dab6f",
  reframe: "#c46b6b",
  maintain: "#7a8a9e",
};

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

function PageChip({ pageId }: { pageId: string }) {
  const short = typeof pageId === "string" ? pageId.slice(0, 8) : pageId;
  return (
    <Link href={`/pages/${pageId}`} className="trace-page-chip">
      {short}
    </Link>
  );
}

function PageList({ pageIds }: { pageIds: string[] }) {
  if (!pageIds || pageIds.length === 0)
    return <span className="trace-empty">none</span>;
  return (
    <span className="trace-page-list">
      {pageIds.map((id) => (
        <PageChip key={id} pageId={id} />
      ))}
    </span>
  );
}

function MoveRow({ moveType, summary }: { moveType: string; summary: string }) {
  const isCreate = moveType.startsWith("CREATE_");
  const isLink = moveType.startsWith("LINK_");
  const isSupersede = moveType === "SUPERSEDE_PAGE";
  const isHypothesis = moveType === "PROPOSE_HYPOTHESIS";

  const typeClass = isCreate
    ? "trace-move-create"
    : isLink
      ? "trace-move-link"
      : isSupersede
        ? "trace-move-supersede"
        : isHypothesis
          ? "trace-move-hypothesis"
          : "trace-move-default";

  return (
    <div className="trace-move-row">
      <span className={`trace-move-type ${typeClass}`}>
        {moveType.replace(/_/g, " ").toLowerCase()}
      </span>
      {summary && <span className="trace-move-summary">{summary}</span>}
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

  return (
    <div
      className={`trace-event ${isWarning ? "trace-event-warning" : ""} ${isError ? "trace-event-error" : ""}`}
    >
      <div className="trace-event-header">
        <span className="trace-event-label">{event.event.replace(/_/g, " ")}</span>
        <span className="trace-event-time">{formatTime(event.ts)}</span>
      </div>

      {event.event === "context_built" && (
        <div className="trace-event-body">
          {(event.working_context_page_ids ?? []).length > 0 && (
            <div className="trace-kv">
              <span className="trace-kv-key">working context</span>
              <PageList pageIds={event.working_context_page_ids ?? []} />
            </div>
          )}
          {(event.preloaded_page_ids ?? []).length > 0 && (
            <div className="trace-kv">
              <span className="trace-kv-key">preloaded</span>
              <PageList pageIds={event.preloaded_page_ids ?? []} />
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

      {(event.event === "phase1_loaded" || event.event === "phase2_loaded") && (
        <div className="trace-event-body">
          <PageList pageIds={event.page_ids ?? []} />
        </div>
      )}

      {event.event === "moves_executed" && (
        <div className="trace-event-body">
          {(event.moves ?? [])
            .filter((m) => m.type !== "LOAD_PAGE")
            .map((m, i) => (
              <MoveRow key={i} moveType={m.type} summary={m.summary || ""} />
            ))}
          {(event.created_page_ids ?? []).length > 0 && (
            <div className="trace-kv" style={{ marginTop: "6px" }}>
              <span className="trace-kv-key">created</span>
              <PageList pageIds={event.created_page_ids ?? []} />
            </div>
          )}
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

      {event.event === "llm_exchange" && (
        <div className="trace-event-body">
          <span className="trace-exchange-info">
            {event.phase} r{event.round}
            {event.input_tokens != null && (
              <span className="trace-token-count">
                {event.input_tokens.toLocaleString()}/{event.output_tokens?.toLocaleString()} tok
              </span>
            )}
          </span>
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
  const [showExchanges, setShowExchanges] = useState(false);
  const { call, events, children } = trace;
  const shortId = call.id.slice(0, 8);
  const duration = getDuration(call);
  const accent = CALL_TYPE_ACCENT[call.call_type] || "#7a8a9e";

  const warningCount = events.filter((e) => e.event === "warning").length;
  const errorCount = events.filter((e) => e.event === "error").length;

  const skipEvents = new Set(["dispatches_planned", "dispatch_executed"]);
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
        } as React.CSSProperties
      }
    >
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="trace-call-header"
      >
        <span className="trace-call-accent" style={{ backgroundColor: accent }} />
        <span className="trace-call-type" style={{ color: accent }}>
          {call.call_type}
        </span>
        <span className="trace-call-id">{shortId}</span>
        <span className="trace-call-meta">
          <StatusDot status={call.status} />
          <span className="trace-call-status">{call.status}</span>
          {duration && <span className="trace-call-duration">{duration}</span>}
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
                          {!ex && " (skipped)"}
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

          <button
            onClick={() => setShowExchanges(!showExchanges)}
            className="trace-toggle-exchanges"
          >
            {showExchanges ? "\u2013 Hide" : "+ Show"} LLM exchanges
          </button>
          {showExchanges && <LLMExchangeDetail callId={call.id} />}

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
