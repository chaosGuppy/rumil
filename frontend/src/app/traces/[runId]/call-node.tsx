"use client";

import { createContext, memo, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import type {
  CallNodeOut,
  CallSummary,
  DispatchExecutedEventOut,
  DispatchesPlannedEventOut,
  GetCallEventsApiCallsCallIdEventsGetResponse,
  LlmExchangeOut,
  PageRef,
} from "@/api/types.gen";
import { CLIENT_API_BASE as QUERY_API_BASE } from "@/api-config";
import { clientFetch } from "@/lib/client-fetch";
import { useStagedRun } from "@/lib/staged-run-context";
import { withStagedRun } from "@/lib/staged-run-href";
import { traceKeys } from "@/lib/queries";
import type { SequenceNode } from "./trace-viewer";
import { ForkPanel, ForkTrigger } from "./fork-panel";

type TraceEvent = GetCallEventsApiCallsCallIdEventsGetResponse[number];

export type TreeNode = {
  node: CallNodeOut;
  children: TreeNode[];
  sequences: SequenceNode[];
};

async function fetchCallEvents(callId: string): Promise<TraceEvent[]> {
  const res = await clientFetch(`${QUERY_API_BASE}/api/calls/${callId}/events`);
  if (!res.ok) throw new Error(`Failed to fetch events: ${res.status}`);
  return res.json();
}

function useCallEvents(callId: string, enabled: boolean, isComplete: boolean) {
  return useQuery({
    queryKey: traceKeys.callEvents(callId),
    queryFn: () => fetchCallEvents(callId),
    enabled,
    staleTime: isComplete ? Infinity : 0,
    refetchInterval: isComplete ? false : 5000,
  });
}

const CALL_TYPE_ACCENT: Record<string, string> = {
  find_considerations: "#5b8def",
  assess: "#a07cdf",
  prioritization: "#d4943a",
  initial_prioritization: "#e8a64d",
  main_phase_prioritization: "#b87320",
  recurse: "#e8853a",
  ingest: "#4dab6f",
  reframe: "#c46b6b",
  maintain: "#7a8a9e",
  summarize: "#8a9e7a",
  scout_subquestions: "#3d8cb5",
  scout_estimates: "#6b9fd4",
  scout_hypotheses: "#4d8fba",
  scout_analogies: "#5498c8",
  scout_paradigm_cases: "#3b7fa8",
  scout_factchecks: "#4793bf",
  scout_c_how_true: "#4dab6f",
  scout_c_how_false: "#c46b6b",
  scout_c_cruxes: "#d4943a",
  scout_c_relevant_evidence: "#6b9fd4",
  scout_c_stress_test_cases: "#b48ad4",
  web_research: "#c4884d",
  evaluate: "#d46b9f",
};

function displayCallType(call: {
  call_type: string;
  call_params?: { [key: string]: unknown } | null;
}): string {
  if (call.call_type === "prioritization" && call.call_params) {
    const phase = call.call_params.phase;
    if (phase === "initial") return "initial_prioritization";
    if (phase === "main_phase") return "main_phase_prioritization";
  }
  return call.call_type;
}

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

function getDuration(call: { created_at: string; completed_at?: string | null }): string | null {
  if (!call.created_at || !call.completed_at) return null;
  const start = new Date(call.created_at).getTime();
  const end = new Date(call.completed_at).getTime();
  const secs = Math.round((end - start) / 1000);
  if (secs < 60) return `${secs}s`;
  return `${Math.floor(secs / 60)}m${secs % 60}s`;
}

function PageChip({ page }: { page: PageRef }) {
  const short = page.id.slice(0, 8);
  const label = page.headline || short;
  const { activeStagedRunId } = useStagedRun();
  const href = withStagedRun(`/pages/${page.id}`, activeStagedRunId);
  return (
    <Link href={href} className="trace-page-chip" title={short}>
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
        <PageChip key={`${p.id}-${i}`} page={p} />
      ))}
    </span>
  );
}

type CtxTierKey = "full" | "distillation" | "abstract" | "summary";

const CTX_TIER_ORDER: { key: CtxTierKey; label: string }[] = [
  { key: "full", label: "full" },
  { key: "distillation", label: "distillation" },
  { key: "abstract", label: "abstract" },
  { key: "summary", label: "summary" },
];

function ContextBuiltBody({
  event,
}: {
  event: Extract<TraceEvent, { event: "context_built" }>;
}) {
  const working = event.working_context_page_ids ?? [];
  const preloaded = event.preloaded_page_ids ?? [];
  const scopeLinked = event.scope_linked_pages ?? [];
  const budgetUsage = event.budget_usage ?? {};
  const tierPages: Record<CtxTierKey, PageRef[]> = {
    full: event.full_pages ?? [],
    distillation: event.distillation_pages ?? [],
    abstract: event.abstract_pages ?? [],
    summary: event.summary_pages ?? [],
  };
  const populatedTiers = CTX_TIER_ORDER.filter(
    (t) => tierPages[t.key].length > 0 || (budgetUsage[t.key] ?? 0) > 0,
  );
  const totalTierChars = CTX_TIER_ORDER.reduce(
    (sum, t) => sum + (budgetUsage[t.key] ?? 0),
    0,
  );
  const uniquePageIds = new Set<string>();
  for (const t of populatedTiers) for (const p of tierPages[t.key]) uniquePageIds.add(p.id);
  for (const p of scopeLinked) uniquePageIds.add(p.id);
  for (const p of preloaded) uniquePageIds.add(p.id);
  if (populatedTiers.length === 0) for (const p of working) uniquePageIds.add(p.id);
  const totalPages = uniquePageIds.size;
  const promptChars =
    event.context_text_chars ?? (event.context_text?.length ?? 0);

  return (
    <div className="trace-event-body">
      <div className="trace-ctx-totals">
        <span className="trace-ctx-totals-pages">
          {totalPages} page{totalPages === 1 ? "" : "s"}
        </span>
        {promptChars > 0 && (
          <>
            <span className="trace-ctx-totals-sep">·</span>
            <span className="trace-ctx-totals-chars">
              {promptChars.toLocaleString()} prompt ch
            </span>
          </>
        )}
        {totalTierChars > 0 && totalTierChars !== promptChars && (
          <>
            <span className="trace-ctx-totals-sep">·</span>
            <span className="trace-ctx-totals-chars">
              {totalTierChars.toLocaleString()} tiered ch
            </span>
          </>
        )}
      </div>

      {totalTierChars > 0 && (
        <div
          className="trace-ctx-budget-bar"
          title={`tiered budget: ${totalTierChars.toLocaleString()} chars`}
        >
          {populatedTiers.map((t) => {
            const chars = budgetUsage[t.key] ?? 0;
            if (chars <= 0) return null;
            return (
              <div
                key={t.key}
                className={`trace-ctx-budget-seg trace-ctx-budget-seg--${t.key}`}
                style={{ flexGrow: chars }}
                title={`${t.label}: ${chars.toLocaleString()} ch`}
              />
            );
          })}
        </div>
      )}

      {populatedTiers.map((t) => {
        const pages = tierPages[t.key];
        const chars = budgetUsage[t.key] ?? 0;
        return (
          <div key={t.key} className="trace-ctx-tier">
            <span className="trace-ctx-tier-label">
              <span
                className={`trace-ctx-tier-swatch trace-ctx-tier-swatch--${t.key}`}
              />
              {t.label}
              <span className="trace-ctx-tier-count">({pages.length})</span>
            </span>
            <PageList pages={pages} />
            <span className="trace-ctx-tier-chars">
              {chars > 0 ? `${chars.toLocaleString()} ch` : "—"}
            </span>
          </div>
        );
      })}

      {populatedTiers.length === 0 && working.length > 0 && (
        <div className="trace-ctx-tier trace-ctx-tier--flat">
          <span className="trace-ctx-tier-label">
            <span className="trace-ctx-tier-swatch trace-ctx-tier-swatch--flat" />
            working context
            <span className="trace-ctx-tier-count">({working.length})</span>
          </span>
          <PageList pages={working} />
          <span className="trace-ctx-tier-chars">—</span>
        </div>
      )}

      {scopeLinked.length > 0 && (
        <div className="trace-ctx-tier trace-ctx-tier--scope-linked">
          <span className="trace-ctx-tier-label">
            <span className="trace-ctx-tier-swatch trace-ctx-tier-swatch--scope-linked" />
            scope-linked
            <span className="trace-ctx-tier-count">({scopeLinked.length})</span>
          </span>
          <PageList pages={scopeLinked} />
          <span className="trace-ctx-tier-chars">—</span>
        </div>
      )}

      {preloaded.length > 0 && (
        <div className="trace-ctx-tier trace-ctx-tier--preloaded">
          <span className="trace-ctx-tier-label">
            <span className="trace-ctx-tier-swatch trace-ctx-tier-swatch--preloaded" />
            preloaded
            <span className="trace-ctx-tier-count">({preloaded.length})</span>
          </span>
          <PageList pages={preloaded} />
          <span className="trace-ctx-tier-chars">—</span>
        </div>
      )}

      {event.source_page_id && (
        <div className="trace-kv">
          <span className="trace-kv-key">source</span>
          <Link
            href={`/pages/${event.source_page_id}`}
            className="trace-kv-value trace-ctx-source-link"
          >
            {event.source_page_id.slice(0, 8)}
          </Link>
        </div>
      )}

      {event.context_text && (
        <details className="trace-ctx-prompt">
          <summary className="trace-ctx-prompt-summary">
            <span className="trace-ctx-prompt-caret" />
            context text
            <span className="trace-ctx-prompt-chars">
              {promptChars.toLocaleString()} ch
            </span>
          </summary>
          <pre className="trace-ctx-prompt-body">{event.context_text}</pre>
        </details>
      )}

      {event.budget != null && (
        <div className="trace-kv">
          <span className="trace-kv-key">budget</span>
          <span className="trace-kv-value">{event.budget}</span>
        </div>
      )}
    </div>
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
  const isLoad = moveType === "LOAD_PAGE";
  const isChange = moveType === "CHANGE_LINK_ROLE";
  const isRemove = moveType === "REMOVE_LINK";

  const typeClass = isCreate
    ? "trace-move-create"
    : isLink
      ? "trace-move-link"
      : isLoad
          ? "trace-move-load"
          : isChange
            ? "trace-move-change"
            : isRemove
              ? "trace-move-remove"
              : "trace-move-default";

  const hasRefs = pageRefs && pageRefs.length > 0;

  if (isChange && extra?.old_role && extra?.new_role) {
    const fromPage = extra.from_page as { id: string; headline: string } | undefined;
    const toPage = extra.to_page as { id: string; headline: string } | undefined;
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
    const fromPage = extra.from_page as { id: string; headline: string } | undefined;
    const toPage = extra.to_page as { id: string; headline: string } | undefined;
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
          {pageRefs.map((p, i) => (
            <PageChip key={`${p.id}-${i}`} page={p} />
          ))}
        </span>
      )}
    </div>
  );
}

import { CLIENT_API_BASE as API_BASE } from "@/api-config";

function TextOverlay({
  label,
  content,
  onClose,
}: {
  label: string;
  content: string;
  onClose: () => void;
}) {
  return (
    <div className="trace-overlay-backdrop" onClick={onClose}>
      <div
        className="trace-overlay-panel"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="trace-overlay-header">
          <span className="trace-overlay-label">{label}</span>
          <span className="trace-overlay-meta">
            {content.length.toLocaleString()} chars
          </span>
          <button className="trace-overlay-close" onClick={onClose}>
            {"\u2715"}
          </button>
        </div>
        <pre className="trace-overlay-content">{content}</pre>
      </div>
    </div>
  );
}

function CollapsiblePre({
  label,
  content,
}: {
  label: string;
  content: string | null;
}) {
  const [open, setOpen] = useState(false);
  const [overlayOpen, setOverlayOpen] = useState(false);
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
        {open && (
          <span
            className="trace-expand-btn"
            role="button"
            title="Expand to full view"
            onClick={(e) => {
              e.stopPropagation();
              setOverlayOpen(true);
            }}
          >
            {"\u2922"}
          </span>
        )}
      </button>
      {open && (
        <pre className="trace-collapsible-content">{content}</pre>
      )}
      {!open && content.length > 200 && (
        <pre className="trace-collapsible-preview">
          {content.slice(0, 200)}...
        </pre>
      )}
      {overlayOpen && (
        <TextOverlay
          label={label}
          content={content}
          onClose={() => setOverlayOpen(false)}
        />
      )}
    </div>
  );
}

function blockLabel(block: Record<string, unknown>): string {
  const t = String(block.type ?? "unknown");
  if (t === "text") return "text";
  if (t === "tool_use") return `tool_use: ${block.name}`;
  if (t === "tool_result") return `tool_result`;
  if (t === "server_tool_use") return `server: ${block.name}`;
  if (t === "web_search_tool_result") return "web_search_result";
  return t;
}

function blockContent(block: Record<string, unknown>): string {
  const t = String(block.type ?? "");
  if (t === "text") return String(block.text ?? "");
  if (t === "tool_use")
    return JSON.stringify(block.input, null, 2);
  if (t === "tool_result") {
    const c = block.content;
    return typeof c === "string" ? c : JSON.stringify(c, null, 2);
  }
  if (t === "server_tool_use")
    return JSON.stringify(block.input, null, 2);
  if (t === "web_search_tool_result")
    return JSON.stringify(block.content, null, 2);
  return JSON.stringify(block, null, 2);
}

function blockTypeClass(block: Record<string, unknown>): string {
  const t = String(block.type ?? "");
  if (t === "text") return "trace-block-text";
  if (t === "tool_use") return "trace-block-tool-use";
  if (t === "tool_result") return "trace-block-tool-result";
  if (t === "server_tool_use") return "trace-block-server-tool";
  if (t === "web_search_tool_result") return "trace-block-web-result";
  return "trace-block-default";
}

function ContentBlock({ block }: { block: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  const [overlayOpen, setOverlayOpen] = useState(false);
  const label = blockLabel(block);
  const content = blockContent(block);
  const chars = content.length;
  const approxTokens = Math.round(chars / 4);
  const typeClass = blockTypeClass(block);

  return (
    <div className={`trace-content-block ${typeClass}`}>
      <button
        className="trace-block-toggle"
        onClick={() => setOpen(!open)}
      >
        <span className="trace-block-chevron">{open ? "\u25BC" : "\u25B6"}</span>
        <span className="trace-block-label">{label}</span>
        <span className="trace-block-size">
          {chars.toLocaleString()} chars
          <span className="trace-block-tokens">~{compactTokens(approxTokens)} tok</span>
        </span>
        {chars > 10000 && (
          <span
            className="trace-block-size-bar"
            title={`${chars.toLocaleString()} chars`}
          >
            <span
              className="trace-block-size-fill"
              style={{ width: `${Math.min(100, chars / 1000)}%` }}
            />
          </span>
        )}
        {open && (
          <span
            className="trace-expand-btn"
            role="button"
            title="Expand to full view"
            onClick={(e) => {
              e.stopPropagation();
              setOverlayOpen(true);
            }}
          >
            {"\u2922"}
          </span>
        )}
      </button>
      {open && (
        <pre className="trace-collapsible-content">{content}</pre>
      )}
      {!open && chars > 200 && (
        <pre className="trace-collapsible-preview">
          {content.slice(0, 200)}...
        </pre>
      )}
      {overlayOpen && (
        <TextOverlay
          label={label}
          content={content}
          onClose={() => setOverlayOpen(false)}
        />
      )}
    </div>
  );
}

function formatMessageContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content))
    return content.map((b) => {
      const block = b as Record<string, unknown>;
      return blockContent(block);
    }).join('\n');
  return String(content ?? "");
}

function totalChars(content: unknown): number {
  if (typeof content === "string") return content.length;
  if (Array.isArray(content))
    return content.reduce((sum, b) => sum + blockContent(b as Record<string, unknown>).length, 0);
  return String(content ?? "").length;
}

function MessageTurn({ msg, index }: { msg: Record<string, unknown>; index: number }) {
  const [overlayOpen, setOverlayOpen] = useState(false);
  const role = String(msg.role ?? "unknown");
  const content = msg.content;
  const blocks = Array.isArray(content) ? content as Array<Record<string, unknown>> : null;
  const chars = totalChars(content);
  const approxTokens = Math.round(chars / 4);

  return (
    <details key={index} className="trace-message-turn">
      <summary className={`trace-message-role trace-role-${role}`}>
        {role}
        <span className="trace-collapsible-meta">
          {chars.toLocaleString()} chars
          <span className="trace-block-tokens">~{compactTokens(approxTokens)} tok</span>
          {blocks && <span className="trace-block-count">{blocks.length} blocks</span>}
        </span>
        <span
          className="trace-expand-btn"
          role="button"
          title="Expand to full view"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            setOverlayOpen(true);
          }}
        >
          {"\u2922"}
        </span>
      </summary>
      {blocks ? (
        <div className="trace-block-list">
          {blocks.map((block, i) => (
            <ContentBlock key={i} block={block} />
          ))}
        </div>
      ) : (
        <pre className="trace-collapsible-content">
          {typeof content === "string" ? content : JSON.stringify(content, null, 2)}
        </pre>
      )}
      {overlayOpen && (
        <TextOverlay
          label={`${role} message (all blocks)`}
          content={formatMessageContent(content)}
          onClose={() => setOverlayOpen(false)}
        />
      )}
    </details>
  );
}

function MessageThread({ messages }: { messages: Array<Record<string, unknown>> }) {
  return (
    <div className="trace-message-thread">
      {messages.map((msg, i) => (
        <MessageTurn key={i} msg={msg} index={i} />
      ))}
    </div>
  );
}

function WebSearchResultEntry({ tc }: { tc: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  const [overlayOpen, setOverlayOpen] = useState(false);
  const contentJson = JSON.stringify(tc.content, null, 2);
  const chars = contentJson.length;
  const approxTokens = Math.round(chars / 4);

  return (
    <div className="trace-content-block trace-block-web-result">
      <button className="trace-block-toggle" onClick={() => setOpen(!open)}>
        <span className="trace-block-chevron">{open ? "\u25BC" : "\u25B6"}</span>
        <span className="trace-block-label">web_search_result</span>
        <span className="trace-block-size">
          {chars.toLocaleString()} chars
          <span className="trace-block-tokens">~{compactTokens(approxTokens)} tok</span>
        </span>
        {chars > 10000 && (
          <span className="trace-block-size-bar" title={`${chars.toLocaleString()} chars`}>
            <span
              className="trace-block-size-fill"
              style={{ width: `${Math.min(100, chars / 1000)}%` }}
            />
          </span>
        )}
        {open && (
          <span
            className="trace-expand-btn"
            role="button"
            title="Expand to full view"
            onClick={(e) => { e.stopPropagation(); setOverlayOpen(true); }}
          >
            {"\u2922"}
          </span>
        )}
      </button>
      {open && <pre className="trace-collapsible-content">{contentJson}</pre>}
      {!open && chars > 200 && (
        <pre className="trace-collapsible-preview">{contentJson.slice(0, 200)}...</pre>
      )}
      {overlayOpen && (
        <TextOverlay
          label="web_search_result"
          content={contentJson}
          onClose={() => setOverlayOpen(false)}
        />
      )}
    </div>
  );
}

function ToolCallsList({ toolCalls }: { toolCalls: Array<Record<string, unknown>> }) {
  const serverToolUses = toolCalls.filter((tc) => !tc.type);
  const webResults = toolCalls.filter((tc) => tc.type === "web_search_tool_result");
  const totalResultChars = webResults.reduce(
    (sum, tc) => sum + JSON.stringify(tc.content).length, 0,
  );

  return (
    <div className="trace-tool-calls">
      <div className="trace-tool-calls-label">
        Tool calls ({serverToolUses.length})
        {webResults.length > 0 && (
          <span className="trace-block-size" style={{ marginLeft: 8 }}>
            + {webResults.length} web results ({totalResultChars.toLocaleString()} chars,{" "}
            ~{compactTokens(Math.round(totalResultChars / 4))} tok)
          </span>
        )}
      </div>
      {serverToolUses.map((tc, i) => (
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
      {webResults.map((tc, i) => (
        <WebSearchResultEntry key={`wr-${i}`} tc={tc} />
      ))}
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
        <ToolCallsList toolCalls={detail.tool_calls} />
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

const EventSection = memo(function EventSection({ event }: { event: TraceEvent }) {
  const isWarning = event.event === "warning";
  const isError = event.event === "error";
  const isExchange = event.event === "llm_exchange";

  const [exchangeDetail, setExchangeDetail] = useState<LlmExchangeOut | null>(null);
  const [exchangeOpen, setExchangeOpen] = useState(false);
  const [exchangeLoading, setExchangeLoading] = useState(false);
  const [forkOpen, setForkOpen] = useState(false);

  async function toggleExchange() {
    if (!isExchange) return;
    if (exchangeDetail) {
      setExchangeOpen(!exchangeOpen);
      return;
    }
    setExchangeLoading(true);
    try {
      const res = await clientFetch(
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
            {event.langfuse_trace_url && (
              <a
                href={event.langfuse_trace_url}
                target="_blank"
                rel="noopener noreferrer"
                onClick={(e) => e.stopPropagation()}
                className="trace-langfuse-link"
                title="View this LLM call in Langfuse"
              >
                Langfuse ↗
              </a>
            )}
          </span>
        )}
        <span className="trace-event-time" suppressHydrationWarning>{formatTime(event.ts)}</span>
      </div>

      {isExchange && exchangeOpen && exchangeDetail && (
        <ExchangeDetail detail={exchangeDetail} />
      )}

      {isExchange && exchangeOpen && exchangeDetail && (
        <ForkTrigger
          exchangeId={event.exchange_id}
          loadedDetail={exchangeDetail}
          open={forkOpen}
          onOpen={() => setForkOpen((v) => !v)}
        />
      )}
      {isExchange && forkOpen && exchangeDetail && (
        <ForkPanel
          exchangeId={event.exchange_id}
          exchangeDetail={exchangeDetail}
          onClose={() => setForkOpen(false)}
        />
      )}

      {event.event === "context_built" && <ContextBuiltBody event={event} />}

      {event.event === "moves_executed" && (
        <div className="trace-event-body">
          {(event.moves ?? []).map((m, i) => (
            <MoveRow
              key={i}
              moveType={m.type}
              summary={m.headline || ""}
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

      {event.event === "experimental_scoring_completed" && (
        <div className="trace-event-body">
          {(event.subquestion_scores ?? []).length > 0 && (
            <div className="trace-scoring-section">
              <div className="trace-kv">
                <span className="trace-kv-key">subquestion scores</span>
              </div>
              {(event.subquestion_scores ?? []).map((s, i) => (
                <div key={i} className="trace-score-row">
                  <span className="trace-score-headline">{s.headline || s.question_id.slice(0, 8)}</span>
                  {s.impact_curve && (
                    <span className="trace-score-reasoning" style={{ whiteSpace: 'pre-wrap' }}>{s.impact_curve}</span>
                  )}
                </div>
              ))}
            </div>
          )}
          {(event.per_type_fruit ?? []).length > 0 && (
            <div className="trace-scoring-section">
              <div className="trace-kv">
                <span className="trace-kv-key">per-scout-type fruit</span>
              </div>
              {(event.per_type_fruit ?? []).map((s, i) => (
                <div key={i} className="trace-fruit-type-row">
                  <div className="trace-fruit-type-header">
                    <span className="trace-score-headline">{s.call_type}</span>
                    <span className="trace-kv-value">{s.fruit}/10</span>
                  </div>
                  {s.reasoning && (
                    <div className="trace-score-reasoning">{s.reasoning}</div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {event.event === "scoring_completed" && (
        <div className="trace-event-body">
          {(event.subquestion_scores ?? []).length > 0 && (
            <div className="trace-scoring-section">
              <div className="trace-kv">
                <span className="trace-kv-key">subquestion scores</span>
              </div>
              {(event.subquestion_scores ?? []).map((s, i) => (
                <div key={i} className="trace-score-row">
                  <span className="trace-score-headline">{s.headline || s.question_id.slice(0, 8)}</span>
                  <span className="trace-kv-value">impact_on_q={s.impact_on_question ?? s.impact ?? 0} broader={s.broader_impact ?? 0} fruit={s.fruit}</span>
                  {s.reasoning && (
                    <span className="trace-score-reasoning">{s.reasoning}</span>
                  )}
                </div>
              ))}
            </div>
          )}
          {(event.claim_scores ?? []).length > 0 && (
            <div className="trace-scoring-section">
              <div className="trace-kv">
                <span className="trace-kv-key">claim scores</span>
              </div>
              {(event.claim_scores ?? []).map((s, i) => (
                <div key={i} className="trace-score-row">
                  <span className="trace-score-headline">{s.headline || s.page_id.slice(0, 8)}</span>
                  <span className="trace-kv-value">impact_on_q={s.impact_on_question ?? s.impact ?? 0} broader={s.broader_impact ?? 0} fruit={s.fruit}</span>
                  {s.reasoning && (
                    <span className="trace-score-reasoning">{s.reasoning}</span>
                  )}
                </div>
              ))}
            </div>
          )}
          {(event.per_type_fruit ?? []).length > 0 ? (
            <div className="trace-scoring-section">
              <div className="trace-kv">
                <span className="trace-kv-key">per-scout-type fruit</span>
              </div>
              {(event.per_type_fruit ?? []).map((s, i) => (
                <div key={i} className="trace-fruit-type-row">
                  <div className="trace-fruit-type-header">
                    <span className="trace-score-headline">{s.call_type}</span>
                    <span className="trace-kv-value">{s.fruit}/10</span>
                  </div>
                  {s.reasoning && (
                    <div className="trace-score-reasoning">{s.reasoning}</div>
                  )}
                </div>
              ))}
              {event.dispatch_guidance && (
                <div className="trace-kv trace-kv-block" style={{ marginTop: 6 }}>
                  <span className="trace-kv-key">dispatch guidance</span>
                  <span className="trace-kv-value" style={{ whiteSpace: 'pre-wrap' }}>{event.dispatch_guidance}</span>
                </div>
              )}
            </div>
          ) : event.parent_fruit != null && (
            <div className="trace-kv">
              <span className="trace-kv-key">parent fruit</span>
              <span className="trace-kv-value">{event.parent_fruit}/10</span>
              {event.parent_fruit_reasoning && (
                <span className="trace-score-reasoning">{event.parent_fruit_reasoning}</span>
              )}
            </div>
          )}
        </div>
      )}

      {event.event === "autocompact" && (
        <div className="trace-event-body trace-info-text">
          Context was automatically condensed mid-run (agent {event.agent_id.slice(0, 8)})
        </div>
      )}

      {event.event === "warning" && (
        <div className="trace-event-body trace-warning-text">
          {event.message}
        </div>
      )}

      {event.event === "error" && (
        <div className="trace-event-body trace-error-text">
          {event.phase && (
            <span className="trace-error-phase">{event.phase}</span>
          )}
          {event.message}
        </div>
      )}

      {event.event === "dispatches_planned" && (
        <div className="trace-event-body">
          {(event.dispatches ?? []).map((d, i) => {
            const isRecurse = d.call_type === "recurse";
            const budget = (d as Record<string, unknown>).budget as number | undefined;
            return (
              <div key={i} className={`trace-dispatch-row${isRecurse ? " trace-dispatch-recurse" : ""}`}>
                <span className="trace-dispatch-index">{i + 1}</span>
                <span
                  className="trace-dispatch-type"
                  style={{ color: CALL_TYPE_ACCENT[d.call_type] || "#7a8a9e" }}
                >
                  {isRecurse ? "recurse" : d.call_type}
                </span>
                {isRecurse && budget != null && (
                  <span className="trace-dispatch-budget">budget {budget}</span>
                )}
                {d.reason ? (
                  <span className="trace-dispatch-reason">{String(d.reason)}</span>
                ) : null}
              </div>
            );
          })}
        </div>
      )}

      {event.event === "explore_page" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">page</span>
            <span className="trace-kv-value">
              <PageChip page={{ id: event.page_id, headline: event.page_headline }} />
            </span>
          </div>
          {event.response && (
            <div className="trace-kv trace-kv-block">
              <span className="trace-kv-key">response</span>
              <span className="trace-kv-value trace-kv-pre">{event.response}</span>
            </div>
          )}
        </div>
      )}

      {event.event === "render_question_subgraph" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">page</span>
            <span className="trace-kv-value">
              <PageChip page={{ id: event.page_id, headline: event.page_headline }} />
            </span>
          </div>
          {event.response && (
            <CollapsiblePre label="Subgraph" content={event.response} />
          )}
        </div>
      )}

      {event.event === "load_page" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">page</span>
            <span className="trace-kv-value">
              <PageChip page={{ id: event.page_id, headline: event.page_headline }} />
            </span>
          </div>
          <div className="trace-kv">
            <span className="trace-kv-key">detail</span>
            <span className="trace-kv-value">{event.detail}</span>
          </div>
          {event.response && (
            <CollapsiblePre label="Content" content={event.response} />
          )}
        </div>
      )}

      {event.event === "link_subquestions_complete" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">proposed</span>
            <span className="trace-kv-value">{event.proposed.length}</span>
          </div>
          {event.proposed.map((p) => (
            <div key={p.id} className="trace-kv">
              <span className="trace-kv-value">
                <PageChip page={{ id: p.id, headline: p.headline }} />
              </span>
            </div>
          ))}
        </div>
      )}

      {event.event === "subagent_started" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">agent type</span>
            <span className="trace-kv-value">{event.agent_type}</span>
          </div>
          <div className="trace-kv">
            <span className="trace-kv-key">child call</span>
            <span className="trace-kv-value"><code>{event.child_call_id.slice(0, 8)}</code></span>
          </div>
          {event.prompt && (
            <div className="trace-kv trace-kv-block">
              <span className="trace-kv-key">prompt</span>
              <span className="trace-kv-value trace-kv-pre">{event.prompt}</span>
            </div>
          )}
        </div>
      )}

      {event.event === "subagent_completed" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">child call</span>
            <span className="trace-kv-value"><code>{event.child_call_id.slice(0, 8)}</code></span>
          </div>
          {(event.input_tokens !== null || event.output_tokens !== null) && (
            <div className="trace-kv">
              <span className="trace-kv-key">tokens</span>
              <span className="trace-kv-value">
                {event.input_tokens ?? 0} in / {event.output_tokens ?? 0} out
                {event.cache_read_input_tokens
                  ? ` (${event.cache_read_input_tokens} cached read)`
                  : ""}
              </span>
            </div>
          )}
          {event.cost_usd !== null && event.cost_usd !== undefined && (
            <div className="trace-kv">
              <span className="trace-kv-key">cost</span>
              <span className="trace-kv-value">${event.cost_usd.toFixed(4)}</span>
            </div>
          )}
          {event.summary && (
            <div className="trace-kv trace-kv-block">
              <span className="trace-kv-key">summary</span>
              <span className="trace-kv-value trace-kv-pre">{event.summary}</span>
            </div>
          )}
        </div>
      )}
      {event.event === "agent_started" && (
        <div className="trace-event-body">
          <CollapsiblePre label="System prompt" content={event.system_prompt} />
          <CollapsiblePre label="User message" content={event.user_message} />
        </div>
      )}
      {event.event === "tool_call" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">tool</span>
            <span className="trace-kv-value"><code>{event.tool_name}</code></span>
          </div>
          {event.tool_input && Object.keys(event.tool_input).length > 0 && (
            <CollapsiblePre label="Input" content={JSON.stringify(event.tool_input, null, 2)} />
          )}
          {event.response && (
            <CollapsiblePre label="Response" content={event.response} />
          )}
        </div>
      )}
      {event.event === "evaluation_complete" && (
        <div className="trace-event-body">
          <CollapsiblePre label="Evaluation" content={event.evaluation} />
        </div>
      )}
      {event.event === "grounding_tasks_generated" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">tasks</span>
            <span className="trace-kv-value">{event.task_count}</span>
          </div>
          {(event.tasks ?? []).map((t: Record<string, unknown>, i: number) => (
            <div key={i} className="trace-score-row">
              <span className="trace-score-headline">{String(t.claim ?? "")}</span>
              {t.grounding_issue ? (
                <span className="trace-score-reasoning">{String(t.grounding_issue)}</span>
              ) : null}
            </div>
          ))}
        </div>
      )}
      {event.event === "reassess_triggered" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">question</span>
            <span className="trace-kv-value">
              <code>{event.question_id.slice(0, 8)}</code>
              {event.question_headline ? ` — ${event.question_headline}` : ""}
            </span>
          </div>
          {event.child_call_id && (
            <div className="trace-kv">
              <span className="trace-kv-key">assess call</span>
              <span className="trace-kv-value"><code>{event.child_call_id.slice(0, 8)}</code></span>
            </div>
          )}
        </div>
      )}
      {event.event === "affected_pages_identified" && (
        <div className="trace-event-body">
          {(event.affected_pages ?? []).map((ap: Record<string, unknown>, i: number) => (
            <div key={i} className="trace-score-row">
              <code>{String(ap.page_id ?? "").slice(0, 8)}</code>
              <span className="trace-score-reasoning">{String(ap.findings_summary ?? "")}</span>
            </div>
          ))}
        </div>
      )}
      {event.event === "update_subgraph_computed" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">nodes</span>
            <span className="trace-kv-value">{event.node_count}</span>
          </div>
          {(event.nodes ?? []).map((n: Record<string, unknown>, i: number) => (
            <div key={i} className="trace-score-row">
              <code>{String(n.page_id ?? "")}</code>
              <span className="trace-kv-value">{String(n.node_type ?? "")}</span>
              {n.has_findings ? <span className="trace-kv-value">[findings]</span> : null}
              <span className="trace-score-reasoning">← {String(n.input_count ?? 0)} deps</span>
            </div>
          ))}
        </div>
      )}
      {event.event === "update_plan_created" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">waves</span>
            <span className="trace-kv-value">{event.wave_count}</span>
          </div>
          <div className="trace-kv">
            <span className="trace-kv-key">operations</span>
            <span className="trace-kv-value">{event.operation_count}</span>
          </div>
          {(event.waves ?? []).map((wave: Record<string, unknown>[], wi: number) => (
            <div key={wi} className="mb-1">
              <div className="text-xs text-muted-foreground">Wave {wi + 1}</div>
              {wave.map((op: Record<string, unknown>, oi: number) => (
                <div key={oi} className="trace-score-row">
                  <code>{String(op.page_id ?? "").slice(0, 8)}</code>
                  <span className="trace-kv-value">{String(op.operation ?? "")}</span>
                  {op.findings_summary ? (
                    <span className="trace-score-reasoning">{String(op.findings_summary).slice(0, 120)}</span>
                  ) : null}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
      {event.event === "claim_reassessed" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">old</span>
            <span className="trace-kv-value"><code>{event.old_page_id.slice(0, 8)}</code></span>
          </div>
          <div className="trace-kv">
            <span className="trace-kv-key">new</span>
            <span className="trace-kv-value"><code>{event.new_page_id.slice(0, 8)}</code></span>
          </div>
          {event.headline ? (
            <div className="trace-kv">
              <span className="trace-kv-key">headline</span>
              <span className="trace-kv-value">{event.headline}</span>
            </div>
          ) : null}
        </div>
      )}
      {event.event === "web_research_complete" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">findings</span>
            <span className="trace-kv-value">{event.task_count}</span>
          </div>
          {(event.findings ?? []).map((f: Record<string, unknown>, i: number) => (
            <div key={i} className="trace-score-row">
              <span className="trace-score-headline">{String(f.claim ?? "")}</span>
              <span className="trace-kv-value">{String(f.findings_length ?? 0)} chars</span>
            </div>
          ))}
        </div>
      )}
      {event.event === "phase_skipped" && (
        <div className="trace-event-body">
          {event.reason && (
            <div className="trace-kv">
              <span className="trace-kv-value">{event.reason}</span>
            </div>
          )}
        </div>
      )}

      {event.event === "update_view_phase_completed" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">processed</span>
            <span className="trace-kv-value">{event.items_processed}</span>
          </div>
          {(event.items_modified ?? 0) > 0 && (
            <div className="trace-kv">
              <span className="trace-kv-key">modified</span>
              <span className="trace-kv-value">{event.items_modified}</span>
            </div>
          )}
          {(event.items_created ?? 0) > 0 && (
            <div className="trace-kv">
              <span className="trace-kv-key">created</span>
              <span className="trace-kv-value">{event.items_created}</span>
            </div>
          )}
          {(event.items_removed ?? 0) > 0 && (
            <div className="trace-kv">
              <span className="trace-kv-key">removed</span>
              <span className="trace-kv-value">{event.items_removed}</span>
            </div>
          )}
        </div>
      )}

      {event.event === "view_created" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">view</span>
            <span className="trace-kv-value">
              <PageChip page={{ id: event.view_id, headline: event.view_headline }} />
            </span>
          </div>
        </div>
      )}

      {event.event === "global_phase_completed" && (
        <div className="trace-event-body">
          {event.outcome && (
            <div className="trace-kv">
              <span className="trace-kv-value">{event.outcome}</span>
            </div>
          )}
        </div>
      )}
      {event.event === "question_dedupe" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">outcome</span>
            <span className="trace-kv-value">{event.outcome.replace(/_/g, " ")}</span>
          </div>
          {event.proposed_headline && (
            <div className="trace-kv">
              <span className="trace-kv-key">proposed</span>
              <span className="trace-kv-value">{event.proposed_headline}</span>
            </div>
          )}
          <div className="trace-kv">
            <span className="trace-kv-key">parent</span>
            <span className="trace-kv-value">
              <code>{event.parent_id.slice(0, 8)}</code>
              {event.parent_headline ? ` — ${event.parent_headline}` : ""}
            </span>
          </div>
          {event.matched_page_id && (
            <div className="trace-kv">
              <span className="trace-kv-key">matched</span>
              <span className="trace-kv-value">
                <code>{event.matched_page_id.slice(0, 8)}</code>
                {event.matched_headline ? ` — ${event.matched_headline}` : ""}
              </span>
            </div>
          )}
          {event.decision_reasoning && (
            <div className="trace-kv">
              <span className="trace-kv-key">reasoning</span>
              <span className="trace-kv-value">{event.decision_reasoning}</span>
            </div>
          )}
          {(event.candidates ?? []).map((c, i) => (
            <div key={i} className="trace-score-row">
              <code>{c.id.slice(0, 8)}</code>
              <span className="trace-score-headline">
                {c.headline}
              </span>
              <span className="trace-kv-value">
                {c.similarity.toFixed(2)} · {c.kept_by_filter ? "kept" : "dropped"}
              </span>
            </div>
          ))}
        </div>
      )}
      {event.event === "impact_filter" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">candidates</span>
            <span className="trace-kv-value">
              {event.candidates_accepted} accepted / {event.candidates_scored} scored
              {event.final_threshold_percentile
                ? ` (threshold p${event.final_threshold_percentile})`
                : ""}
            </span>
          </div>
          <div className="trace-kv">
            <span className="trace-kv-key">chars</span>
            <span className="trace-kv-value">
              {event.inner_context_chars.toLocaleString()} inner
              {" + "}
              {event.accepted_chars.toLocaleString()} added
            </span>
          </div>
          <div className="trace-kv">
            <span className="trace-kv-key">scoring</span>
            <span className="trace-kv-value">{event.scoring_model}</span>
          </div>
          {event.paring_triggered && (
            <div className="trace-kv">
              <span className="trace-kv-key">paring</span>
              <span className="trace-kv-value">
                {event.paring_kept_pages ?? "?"} pages, {(event.paring_kept_chars ?? 0).toLocaleString()} chars
                {event.pare_model ? ` (${event.pare_model})` : ""}
              </span>
            </div>
          )}
        </div>
      )}

      {event.event === "round_started" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">round</span>
            <span className="trace-kv-value">{event.round}</span>
          </div>
        </div>
      )}

      {event.event === "draft_started" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">round</span>
            <span className="trace-kv-value">{event.round}</span>
            {event.model && (
              <>
                <span className="trace-kv-key" style={{ marginLeft: 12 }}>model</span>
                <span className="trace-kv-value"><code>{event.model}</code></span>
              </>
            )}
          </div>
        </div>
      )}

      {event.event === "critique_started" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">round</span>
            <span className="trace-kv-value">{event.round}</span>
            <span className="trace-kv-key" style={{ marginLeft: 12 }}>critic</span>
            <span className="trace-kv-value">{event.critic_index}</span>
            {event.model && (
              <>
                <span className="trace-kv-key" style={{ marginLeft: 12 }}>model</span>
                <span className="trace-kv-value"><code>{event.model}</code></span>
              </>
            )}
          </div>
        </div>
      )}

      {event.event === "edit_started" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">round</span>
            <span className="trace-kv-value">{event.round}</span>
            {event.model && (
              <>
                <span className="trace-kv-key" style={{ marginLeft: 12 }}>model</span>
                <span className="trace-kv-value"><code>{event.model}</code></span>
              </>
            )}
            <span className="trace-kv-key" style={{ marginLeft: 12 }}>current</span>
            <span className="trace-kv-value">{event.current_chars.toLocaleString()} ch</span>
            <span className="trace-kv-key" style={{ marginLeft: 12 }}>critiques</span>
            <span className="trace-kv-value">{event.n_critiques}</span>
          </div>
        </div>
      )}

      {event.event === "draft" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">round</span>
            <span className="trace-kv-value">{event.round}</span>
            {event.model && (
              <>
                <span className="trace-kv-key" style={{ marginLeft: 12 }}>model</span>
                <span className="trace-kv-value"><code>{event.model}</code></span>
              </>
            )}
            {event.draft_chars > 0 && (
              <>
                <span className="trace-kv-key" style={{ marginLeft: 12 }}>chars</span>
                <span className="trace-kv-value">{event.draft_chars.toLocaleString()}</span>
              </>
            )}
          </div>
          {event.draft_text ? (
            <CollapsiblePre label="Draft text" content={event.draft_text} />
          ) : (
            <span className="trace-empty">(empty)</span>
          )}
        </div>
      )}

      {event.event === "critique_round" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">round</span>
            <span className="trace-kv-value">{event.round}</span>
            <span className="trace-kv-key" style={{ marginLeft: 12 }}>critics</span>
            <span className="trace-kv-value">{event.critiques.length}</span>
          </div>
          {event.critiques.length === 0 ? (
            <span className="trace-empty">(no critiques)</span>
          ) : (
            event.critiques.map((c, i) => {
              const critiqueLabel =
                `Critic ${c.critic_index ?? i}`
                + (c.model ? ` · ${c.model}` : "");
              return (
                <div key={i} className="trace-fruit-type-row">
                  <div className="trace-fruit-type-header">
                    <span className="trace-score-headline">{critiqueLabel}</span>
                    {c.critique_text && (
                      <span className="trace-collapsible-meta">
                        {c.critique_text.length.toLocaleString()} ch
                      </span>
                    )}
                  </div>
                  {c.critique_text ? (
                    <CollapsiblePre label="Critique" content={c.critique_text} />
                  ) : (
                    <span className="trace-empty">(empty)</span>
                  )}
                </div>
              );
            })
          )}
        </div>
      )}

      {event.event === "edit" && (
        <div className="trace-event-body">
          <div className="trace-kv">
            <span className="trace-kv-key">round</span>
            <span className="trace-kv-value">{event.round}</span>
            {event.model && (
              <>
                <span className="trace-kv-key" style={{ marginLeft: 12 }}>model</span>
                <span className="trace-kv-value"><code>{event.model}</code></span>
              </>
            )}
            {event.revised_chars > 0 && (
              <>
                <span className="trace-kv-key" style={{ marginLeft: 12 }}>chars</span>
                <span className="trace-kv-value">{event.revised_chars.toLocaleString()}</span>
              </>
            )}
          </div>
          {event.revised_text ? (
            <CollapsiblePre label="Revised text" content={event.revised_text} />
          ) : (
            <span className="trace-empty">(empty)</span>
          )}
        </div>
      )}
    </div>
  );
});

const SEQUENCE_COLORS = [
  "#5b8def", "#a07cdf", "#4dab6f", "#c4884d", "#c46b6b",
  "#3d8cb5", "#d4943a", "#8a9e7a", "#6b9fd4", "#b48ad4",
];

const SequenceGroup = memo(function SequenceGroup({
  sequences,
  depth,
}: {
  sequences: SequenceNode[];
  depth: number;
}) {
  return (
    <div className="trace-sequences">
      {sequences.map((seq, si) => {
        const seqColor = SEQUENCE_COLORS[si % SEQUENCE_COLORS.length];
        return (
          <div
            key={seq.id}
            className="trace-sequence-lane"
            style={{
              borderLeftColor: seqColor,
            }}
          >
            <div className="trace-sequence-header" style={{ color: seqColor }}>
              <span className="trace-sequence-label">
                sequence {si + 1}
              </span>
              <span className="trace-sequence-meta">
                {seq.calls.length} call{seq.calls.length !== 1 ? "s" : ""}
              </span>
            </div>
            {seq.calls.map((t) => (
              <CallNode key={t.node.call.id} tree={t} depth={depth + 1} />
            ))}
          </div>
        );
      })}
    </div>
  );
});

function CopyLinkButton({ anchor }: { anchor: string }) {
  const [copied, setCopied] = useState(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout>>(null);

  const handleCopy = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      const url = `${window.location.origin}${window.location.pathname}#${anchor}`;
      navigator.clipboard.writeText(url).then(() => {
        setCopied(true);
        if (timeoutRef.current) clearTimeout(timeoutRef.current);
        timeoutRef.current = setTimeout(() => setCopied(false), 1500);
      });
    },
    [anchor],
  );

  return (
    <button
      onClick={handleCopy}
      className="trace-copy-link"
      title="Copy link to call"
      aria-label="Copy link to call"
      data-testid={`copy-link-${anchor}`}
    >
      {copied ? (
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="3.5 8.5 6.5 11.5 12.5 4.5" />
        </svg>
      ) : (
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M6.5 9.5a3 3 0 0 0 4.24 0l2-2a3 3 0 0 0-4.24-4.24l-1 1" />
          <path d="M9.5 6.5a3 3 0 0 0-4.24 0l-2 2a3 3 0 0 0 4.24 4.24l1-1" />
        </svg>
      )}
    </button>
  );
}

function treeContainsAnchor(tree: TreeNode, hash: string): boolean {
  const shortId = tree.node.call.id.slice(0, 8);
  if (`call-${shortId}` === hash) return true;
  for (const child of tree.children) {
    if (treeContainsAnchor(child, hash)) return true;
  }
  for (const seq of tree.sequences) {
    for (const seqCall of seq.calls) {
      if (treeContainsAnchor(seqCall, hash)) return true;
    }
  }
  return false;
}

const HashTargetContext = createContext("");

export function HashTargetProvider({ children }: { children: React.ReactNode }) {
  const [hash, setHash] = useState("");

  useEffect(() => {
    setHash(window.location.hash.slice(1));
    const onHashChange = () => setHash(window.location.hash.slice(1));
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  return (
    <HashTargetContext.Provider value={hash}>
      {children}
    </HashTargetContext.Provider>
  );
}

export const CallNode = memo(function CallNode({
  tree,
  depth,
}: {
  tree: TreeNode;
  depth: number;
}) {
  const { node, children, sequences } = tree;
  const { call } = node;
  const shortId = call.id.slice(0, 8);
  const anchor = `call-${shortId}`;
  const hash = useContext(HashTargetContext);
  const isHashTarget = hash === anchor;
  const hasTargetDescendant = useMemo(
    () => !isHashTarget && hash !== "" && treeContainsAnchor(tree, hash),
    [isHashTarget, hash, tree],
  );
  const [isOpen, setIsOpen] = useState(depth === 0 || isHashTarget || hasTargetDescendant);
  const nodeRef = useRef<HTMLDivElement>(null);
  const isComplete = call.status === "complete" || call.status === "failed";
  const queryClient = useQueryClient();
  const prevCompleteRef = useRef(isComplete);
  const { data: events } = useCallEvents(call.id, isOpen, isComplete);

  useEffect(() => {
    if (isComplete && !prevCompleteRef.current) {
      queryClient.invalidateQueries({ queryKey: traceKeys.callEvents(call.id) });
    }
    prevCompleteRef.current = isComplete;
  }, [isComplete, call.id, queryClient]);

  useEffect(() => {
    if (hasTargetDescendant) {
      setIsOpen(true);
    }
  }, [hasTargetDescendant]);

  useEffect(() => {
    if (isHashTarget && nodeRef.current) {
      setIsOpen(true);
      requestAnimationFrame(() => {
        nodeRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }
  }, [isHashTarget]);
  const duration = getDuration(call);
  const callTypeLabel = displayCallType(call);
  const accent = CALL_TYPE_ACCENT[callTypeLabel] || "#7a8a9e";

  const warningCount = useMemo(
    () =>
      events
        ? events.filter((e) => e.event === "warning").length
        : (node.warning_count ?? 0),
    [events, node.warning_count],
  );
  const errorCount = useMemo(
    () =>
      events
        ? events.filter((e) => e.event === "error").length
        : (node.error_count ?? 0),
    [events, node.error_count],
  );

  const displayableEvents = useMemo(() => {
    if (!events) return [];
    const skipEvents = new Set(["dispatches_planned", "dispatch_executed"]);
    return events.filter((e) => !skipEvents.has(e.event));
  }, [events]);

  const dispatchEvents = useMemo(
    () => (events ?? []).filter(
      (e): e is DispatchesPlannedEventOut => e.event === "dispatches_planned",
    ),
    [events],
  );
  const executedMap = useMemo(() => {
    const map = new Map<number, DispatchExecutedEventOut>();
    for (const e of events ?? []) {
      if (e.event === "dispatch_executed") {
        map.set(e.index, e);
      }
    }
    return map;
  }, [events]);

  return (
    <div
      ref={nodeRef}
      id={anchor}
      className={`trace-call-node${isHashTarget ? " trace-call-targeted" : ""}`}
      style={
        {
          "--call-accent": accent,
          marginLeft: depth > 0 ? "20px" : "0",
          borderLeft: `3px solid ${accent}`,
          paddingLeft: "10px",
        } as React.CSSProperties
      }
    >
      <div className="trace-call-header-row">
        <button
          onClick={() => setIsOpen(!isOpen)}
          className="trace-call-header"
        >
          <span className="trace-call-type">
            {callTypeLabel}
          </span>
          {node.scope_page_summary && (
            <span className="trace-call-scope">{node.scope_page_summary}</span>
          )}
          <span className="trace-call-id">{shortId}</span>
          <span className="trace-call-meta">
            <StatusDot status={call.status} />
            <span className="trace-call-status">{call.status}</span>
            {duration && <span className="trace-call-duration">{duration}</span>}
            {call.cost_usd != null && (
              <span className="trace-call-cost">${call.cost_usd.toFixed(4)}</span>
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
        <CopyLinkButton anchor={anchor} />
      </div>

      {isOpen && (
        <div className="trace-call-body">
          {call.call_params && Object.keys(call.call_params).length > 0 && (
            <div className="trace-call-params">
              {Object.entries(call.call_params)
                .filter(
                  ([key]) =>
                    !(call.call_type === "prioritization" && key === "phase"),
                )
                .map(([key, value]) => (
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
                  const isRecurse = d.call_type === "recurse";
                  const accent = CALL_TYPE_ACCENT[d.call_type] || "#7a8a9e";
                  const budget = (d as Record<string, unknown>).budget as number | undefined;
                  const questionHeadline = ex?.question_headline || null;
                  const questionId = (ex?.question_id ?? (d as Record<string, unknown>).question_id) as string | undefined;
                  return (
                    <div key={i} className={`trace-dispatch-item${isRecurse ? " trace-dispatch-recurse" : ""}`}>
                      <div className="trace-dispatch-header">
                        <span className="trace-dispatch-index">{i + 1}</span>
                        {childCallId ? (
                          <a
                            href={`#call-${childCallId.slice(0, 8)}`}
                            className="trace-dispatch-link"
                            style={{ color: accent }}
                            onClick={(e) => {
                              e.preventDefault();
                              document
                                .getElementById(
                                  `call-${childCallId.slice(0, 8)}`,
                                )
                                ?.scrollIntoView({ behavior: "smooth" });
                            }}
                          >
                            {isRecurse ? "recurse" : d.call_type}
                          </a>
                        ) : (
                          <span
                            className={isRecurse ? "trace-dispatch-type" : "trace-dispatch-skipped"}
                            style={{ color: accent }}
                          >
                            {isRecurse ? "recurse" : d.call_type}
                          </span>
                        )}
                        {isRecurse && budget != null && (
                          <span className="trace-dispatch-budget">budget {budget}</span>
                        )}
                        {(questionHeadline || questionId) && (
                          <span
                            className="trace-dispatch-question"
                            title={questionHeadline || questionId}
                          >
                            {questionHeadline || questionId?.slice(0, 8)}
                          </span>
                        )}
                      </div>
                      {d.reason ? (
                        <div className="trace-dispatch-reason">
                          {String(d.reason)}
                        </div>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {!events && (
            <div className="trace-events-loading">Loading events...</div>
          )}

          {displayableEvents.length > 0 && (
            <div className="trace-events">
              {displayableEvents.map((ev, i) => (
                <EventSection key={`${ev.ts}-${i}`} event={ev} />
              ))}
            </div>
          )}

          {sequences.length > 0 && (
            <SequenceGroup sequences={sequences} depth={depth} />
          )}
          {children.length > 0 && (
            <div className="trace-children">
              {children.map((child) => (
                <CallNode key={child.node.call.id} tree={child} depth={depth + 1} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
});
