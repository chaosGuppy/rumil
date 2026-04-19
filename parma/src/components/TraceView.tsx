"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  fetchCallEvents,
  fetchCallLLMExchanges,
  fetchLLMExchange,
  fetchRunTraceTree,
  fetchProjectRuns,
  type LLMExchangeDetail,
  type LLMExchangeSummary,
  type RunListItem,
  type RunTraceTree,
  type TraceCallNode,
  type TraceEvent,
} from "@/lib/api";

// TraceView — the TRACE view mode.
//
// Renders one run's call tree on the left, the selected call's detail on
// the right. Validated against real call 721fad80 (run 8a054890, project
// wave7-smoke, question 8b567a24, claim 5b38de2b) — this is the
// find_considerations call that produced Brian's claim. The implementation
// supports any run structure (recursive dispatch, sequences, stragglers).

export interface TraceViewProps {
  runId: string | null;
  projectId: string;
  // Optional: preselect a specific call when the user enters trace mode
  // via a provenance chip. If absent, we auto-select the root call.
  initialCallId?: string | null;
  // Called when the user picks a run from the no-run picker. Parent
  // bumps the URL via router.replace and rerenders with runId set.
  onSelectRun: (runId: string) => void;
  // Called by the "back to view" link: parent switches back to the
  // previous view mode (panes, article, etc).
  onBack?: () => void;
}

export function TraceView(props: TraceViewProps) {
  if (!props.runId) {
    return (
      <RunPicker
        projectId={props.projectId}
        onSelect={props.onSelectRun}
      />
    );
  }
  return (
    <TraceRunView
      key={props.runId}
      runId={props.runId}
      projectId={props.projectId}
      initialCallId={props.initialCallId}
      onBack={props.onBack}
    />
  );
}

// When trace mode is entered without a run_id, show a list of recent runs
// in the current project. This is a soft on-ramp — most users will land
// in trace mode via a chip with a run already selected.
function RunPicker({
  projectId,
  onSelect,
}: {
  projectId: string;
  onSelect: (runId: string) => void;
}) {
  const [rows, setRows] = useState<RunListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchProjectRuns(projectId)
      .then((r) => {
        if (!cancelled) setRows(r);
      })
      .catch((e) => {
        if (!cancelled) setError(e?.message ?? "failed");
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  if (error) return <div className="trace-pick-error">Failed to load runs: {error}</div>;
  if (!rows) return <div className="trace-pick-loading">Loading runs...</div>;

  const usable = rows.filter((r) => r.run_id);
  if (usable.length === 0) {
    return (
      <div className="trace-pick-empty">
        No runs in this project yet. Dispatch a call first.
      </div>
    );
  }

  return (
    <div className="trace-pick">
      <div className="trace-pick-head">
        <h2 className="trace-pick-title">Runs</h2>
        <p className="trace-pick-sub">
          Pick a run to inspect. Most-recent first.
        </p>
      </div>
      <div className="trace-pick-list">
        {usable.map((r) => {
          const discriminators = extractRunDiscriminators(r.config);
          return (
            <button
              key={r.run_id!}
              className="trace-pick-row"
              onClick={() => onSelect(r.run_id!)}
            >
              <div className="trace-pick-row-head">
                <span className="trace-pick-row-name">
                  {r.name || r.run_id!.slice(0, 8)}
                </span>
                {r.staged && <span className="trace-pick-row-staged">staged</span>}
              </div>
              {r.question_summary && (
                <div className="trace-pick-row-q">{r.question_summary}</div>
              )}
              <div className="trace-pick-row-meta">
                <span>{r.run_id!.slice(0, 8)}</span>
                <span>·</span>
                <span>{formatWhen(r.created_at)}</span>
                {discriminators.map((d) => (
                  <span key={d.key} className="trace-pick-row-meta-chip" title={d.title}>
                    {d.label}
                  </span>
                ))}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// Pull a few human-scannable fields out of a run's captured config so
// adjacent rows with the same truncated name (see ux-review-wave7 #8 —
// all "refine-artifact (strategy_brief): ..." in wave7-smoke) are
// visibly different at a glance. We surface orchestrator (if present),
// model, available_moves preset, and the short git commit. Extra runs
// contribute extra chips automatically — we don't hand-pick further.
function extractRunDiscriminators(
  config: Record<string, unknown> | null,
): Array<{ key: string; label: string; title: string }> {
  if (!config) return [];
  const out: Array<{ key: string; label: string; title: string }> = [];
  const orchestrator = config["orchestrator"];
  if (typeof orchestrator === "string" && orchestrator) {
    out.push({ key: "orchestrator", label: orchestrator, title: `orchestrator: ${orchestrator}` });
  }
  const model = config["model"];
  if (typeof model === "string" && model) {
    const short = model.replace(/^claude-/, "").replace(/-\d{8}$/, "");
    out.push({ key: "model", label: short, title: `model: ${model}` });
  }
  const moves = config["available_moves"];
  if (typeof moves === "string" && moves && moves !== "default") {
    out.push({ key: "moves", label: `moves:${moves}`, title: `available_moves: ${moves}` });
  }
  const commit = config["git_commit"];
  if (typeof commit === "string" && commit) {
    out.push({ key: "commit", label: commit.slice(0, 7), title: `git_commit: ${commit}` });
  }
  return out;
}

function TraceRunView({
  runId,
  projectId,
  initialCallId,
  onBack,
}: {
  runId: string;
  projectId: string;
  initialCallId?: string | null;
  onBack?: () => void;
}) {
  const [tree, setTree] = useState<RunTraceTree | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedCallId, setSelectedCallId] = useState<string | null>(
    initialCallId ?? null,
  );

  useEffect(() => {
    let cancelled = false;
    setTree(null);
    setError(null);
    fetchRunTraceTree(runId)
      .then((t) => {
        if (cancelled) return;
        setTree(t);
        // Auto-select root call if nothing was pre-selected.
        if (!initialCallId && t.calls.length > 0) {
          const root = t.calls.find((c) => !c.call.parent_call_id) ?? t.calls[0];
          setSelectedCallId(root.call.id);
        }
      })
      .catch((e) => {
        if (!cancelled) setError(e?.message ?? "failed");
      });
    return () => {
      cancelled = true;
    };
  }, [runId, initialCallId]);

  // When the consumer pre-selects a call, trust them — don't wait for the
  // tree to load before reflecting that in state.
  useEffect(() => {
    if (initialCallId) setSelectedCallId(initialCallId);
  }, [initialCallId]);

  if (error) {
    return (
      <div className="trace-error">
        Could not load run: {error}
        <br />
        <span className="trace-error-hint">
          Is the rumil API up? (./scripts/dev-api.sh)
        </span>
      </div>
    );
  }
  if (!tree) return <div className="trace-loading">Loading trace...</div>;

  return (
    <div className="trace-layout">
      <aside className="trace-tree-col">
        <TraceHeader
          tree={tree}
          projectId={projectId}
          onBack={onBack}
        />
        <div className="trace-tree-scroll">
          <CallTree
            calls={tree.calls}
            selectedCallId={selectedCallId}
            onSelect={setSelectedCallId}
          />
        </div>
      </aside>
      <section className="trace-detail-col">
        {selectedCallId ? (
          <CallDetail
            callId={selectedCallId}
            tree={tree}
          />
        ) : (
          <div className="trace-empty-detail">
            Pick a call on the left to inspect it.
          </div>
        )}
      </section>
    </div>
  );
}

function TraceHeader({
  tree,
  projectId,
  onBack,
}: {
  tree: RunTraceTree;
  projectId: string;
  onBack?: () => void;
}) {
  // Surface the most useful run-level context at the top: question
  // headline (if present), short run id, total cost, staged flag. This
  // replaces the cross-app jump — Brian never has to guess which run he's
  // looking at.
  const shortRunId = tree.run_id.slice(0, 8);
  return (
    <header className="trace-head">
      <div className="trace-head-row">
        {onBack && (
          <button className="trace-head-back" onClick={onBack} title="Back">
            ← back
          </button>
        )}
        <span className="trace-head-label">trace</span>
        <span className="trace-head-run-id" title={tree.run_id}>
          {shortRunId}
        </span>
        {tree.staged && <span className="trace-head-staged">staged</span>}
        {tree.cost_usd != null && (
          <span className="trace-head-cost">${tree.cost_usd.toFixed(4)}</span>
        )}
      </div>
      {tree.question && (
        <div className="trace-head-question">
          <span className="trace-head-question-label">for</span>
          <span className="trace-head-question-text">
            {tree.question.headline}
          </span>
        </div>
      )}
      <div className="trace-head-project" title={projectId}>
        {projectId.slice(0, 8)} · project
      </div>
    </header>
  );
}

// Node shape we build locally. Tree is materialized once per run load.
interface TreeNode {
  node: TraceCallNode;
  children: TreeNode[];
}

function buildTree(calls: TraceCallNode[]): TreeNode[] {
  const byId = new Map<string, TraceCallNode>();
  const childrenOf = new Map<string, TraceCallNode[]>();
  for (const c of calls) {
    byId.set(c.call.id, c);
    if (c.call.parent_call_id) {
      const list = childrenOf.get(c.call.parent_call_id) ?? [];
      list.push(c);
      childrenOf.set(c.call.parent_call_id, list);
    }
  }
  // Sort children by sequence_position then created_at. The trace-tree
  // endpoint doesn't guarantee order and dispatches within a sequence
  // belong in their declared order.
  const byCreated = (a: TraceCallNode, b: TraceCallNode) =>
    new Date(a.call.created_at).getTime() -
    new Date(b.call.created_at).getTime();
  const orderedKids = (parentId: string): TraceCallNode[] => {
    const kids = [...(childrenOf.get(parentId) ?? [])];
    kids.sort((a, b) => {
      const ap = a.call.sequence_position ?? null;
      const bp = b.call.sequence_position ?? null;
      if (ap != null && bp != null) return ap - bp;
      return byCreated(a, b);
    });
    return kids;
  };
  const toTreeNode = (n: TraceCallNode): TreeNode => ({
    node: n,
    children: orderedKids(n.call.id).map(toTreeNode),
  });
  const roots = calls.filter((c) => !c.call.parent_call_id);
  roots.sort(byCreated);
  return roots.map(toTreeNode);
}

function CallTree({
  calls,
  selectedCallId,
  onSelect,
}: {
  calls: TraceCallNode[];
  selectedCallId: string | null;
  onSelect: (id: string) => void;
}) {
  const roots = useMemo(() => buildTree(calls), [calls]);
  if (roots.length === 0) {
    return (
      <div className="trace-tree-empty">
        No calls recorded for this run yet.
      </div>
    );
  }
  return (
    <ul className="trace-tree">
      {roots.map((r) => (
        <CallTreeNode
          key={r.node.call.id}
          tree={r}
          depth={0}
          selectedCallId={selectedCallId}
          onSelect={onSelect}
        />
      ))}
    </ul>
  );
}

function CallTreeNode({
  tree,
  depth,
  selectedCallId,
  onSelect,
}: {
  tree: TreeNode;
  depth: number;
  selectedCallId: string | null;
  onSelect: (id: string) => void;
}) {
  const { call } = tree.node;
  const isSelected = selectedCallId === call.id;
  const duration = formatDuration(call.created_at, call.completed_at);
  return (
    <li className="trace-tree-item">
      <button
        className={`trace-tree-row ${isSelected ? "is-selected" : ""}`}
        onClick={() => onSelect(call.id)}
        style={{ paddingLeft: `${12 + depth * 14}px` }}
      >
        <span className={`trace-tree-dot status-${call.status}`} />
        <span className="trace-tree-type">{call.call_type}</span>
        <span className="trace-tree-id" title={call.id}>
          {call.id.slice(0, 8)}
        </span>
        <span className="trace-tree-meta">
          {call.cost_usd != null && (
            <span className="trace-tree-cost">${call.cost_usd.toFixed(3)}</span>
          )}
          {duration && <span className="trace-tree-dur">{duration}</span>}
          {tree.node.warning_count > 0 && (
            <span className="trace-tree-warn" title="warnings">
              {tree.node.warning_count}w
            </span>
          )}
          {tree.node.error_count > 0 && (
            <span className="trace-tree-err" title="errors">
              {tree.node.error_count}e
            </span>
          )}
        </span>
      </button>
      {tree.children.length > 0 && (
        <ul className="trace-tree-children">
          {tree.children.map((c) => (
            <CallTreeNode
              key={c.node.call.id}
              tree={c}
              depth={depth + 1}
              selectedCallId={selectedCallId}
              onSelect={onSelect}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

function CallDetail({
  callId,
  tree,
}: {
  callId: string;
  tree: RunTraceTree;
}) {
  const node = useMemo(
    () => tree.calls.find((c) => c.call.id === callId) ?? null,
    [tree, callId],
  );
  const [events, setEvents] = useState<TraceEvent[] | null>(null);
  const [exchanges, setExchanges] = useState<LLMExchangeSummary[] | null>(null);
  const [eventsErr, setEventsErr] = useState<string | null>(null);
  const [exchangesErr, setExchangesErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setEvents(null);
    setExchanges(null);
    setEventsErr(null);
    setExchangesErr(null);
    fetchCallEvents(callId)
      .then((e) => {
        if (!cancelled) setEvents(e);
      })
      .catch((e) => {
        if (!cancelled) setEventsErr(e?.message ?? "failed");
      });
    fetchCallLLMExchanges(callId)
      .then((x) => {
        if (!cancelled) setExchanges(x);
      })
      .catch((e) => {
        if (!cancelled) setExchangesErr(e?.message ?? "failed");
      });
    return () => {
      cancelled = true;
    };
  }, [callId]);

  if (!node) return <div className="trace-empty-detail">Unknown call.</div>;
  const { call } = node;
  const duration = formatDuration(call.created_at, call.completed_at);

  return (
    <div className="trace-detail-scroll">
      <section className="trace-detail-head">
        <div className="trace-detail-type-row">
          <span className={`trace-tree-dot status-${call.status}`} />
          <span className="trace-detail-type">{call.call_type}</span>
          <span className="trace-detail-id" title={call.id}>
            {call.id.slice(0, 8)}
          </span>
          <span className="trace-detail-status">{call.status}</span>
        </div>
        {node.scope_page_summary && (
          <h1 className="trace-detail-scope">{node.scope_page_summary}</h1>
        )}
        <dl className="trace-detail-meta">
          {call.scope_page_id && (
            <MetaRow
              label="scope"
              value={
                <span className="trace-detail-mono">
                  {call.scope_page_id.slice(0, 8)}
                </span>
              }
            />
          )}
          <MetaRow label="created" value={formatWhen(call.created_at)} />
          {call.completed_at && (
            <MetaRow label="completed" value={formatWhen(call.completed_at)} />
          )}
          {duration && <MetaRow label="duration" value={duration} />}
          {call.cost_usd != null && (
            <MetaRow label="cost" value={`$${call.cost_usd.toFixed(4)}`} />
          )}
          {call.call_params && Object.keys(call.call_params).length > 0 && (
            <MetaRow
              label="params"
              value={
                <span className="trace-detail-mono">
                  {compactJson(call.call_params)}
                </span>
              }
            />
          )}
        </dl>
      </section>

      <section className="trace-detail-section">
        <h2 className="trace-detail-section-title">
          Events
          <span className="trace-detail-section-count">
            {events?.length ?? "-"}
          </span>
        </h2>
        {eventsErr && <div className="trace-detail-err">{eventsErr}</div>}
        {!events && !eventsErr && (
          <div className="trace-detail-dim">loading events...</div>
        )}
        {events && events.length === 0 && (
          <div className="trace-detail-dim">No events recorded.</div>
        )}
        {events && events.length > 0 && <EventTimeline events={events} />}
      </section>

      <section className="trace-detail-section">
        <h2 className="trace-detail-section-title">
          LLM exchanges
          <span className="trace-detail-section-count">
            {exchanges?.length ?? "-"}
          </span>
        </h2>
        {exchangesErr && <div className="trace-detail-err">{exchangesErr}</div>}
        {!exchanges && !exchangesErr && (
          <div className="trace-detail-dim">loading exchanges...</div>
        )}
        {exchanges && exchanges.length === 0 && (
          <div className="trace-detail-dim">No LLM exchanges recorded.</div>
        )}
        {exchanges && exchanges.length > 0 && (
          <ExchangeList exchanges={exchanges} />
        )}
      </section>
    </div>
  );
}

function MetaRow({
  label,
  value,
}: {
  label: string;
  value: ReactNode;
}) {
  return (
    <div className="trace-meta-row">
      <dt className="trace-meta-label">{label}</dt>
      <dd className="trace-meta-value">{value}</dd>
    </div>
  );
}

// Event family coloring. We read event.event (the discriminator) and bucket
// into a small set of families: context, mutation (moves/pages/links),
// dispatch, llm, error, other. Each family maps to an existing CSS var.
function eventFamily(event: string): string {
  if (event === "error") return "error";
  if (event === "warning") return "warn";
  if (event === "llm_exchange") return "llm";
  if (event === "context_built" || event === "load_page" || event === "explore_page")
    return "context";
  if (
    event === "moves_executed" ||
    event === "claim_reassessed" ||
    event === "view_created" ||
    event === "link_subquestions_complete"
  )
    return "mutate";
  if (
    event === "dispatches_planned" ||
    event === "dispatch_executed" ||
    event === "subagent_started" ||
    event === "subagent_completed"
  )
    return "dispatch";
  return "other";
}

function EventTimeline({ events }: { events: TraceEvent[] }) {
  return (
    <ol className="trace-event-list">
      {events.map((e, i) => (
        <EventRow key={i} event={e} index={i} />
      ))}
    </ol>
  );
}

function EventRow({ event, index }: { event: TraceEvent; index: number }) {
  const [open, setOpen] = useState(false);
  const family = eventFamily(event.event);
  const summary = eventSummary(event);
  return (
    <li className={`trace-event trace-event-${family}`}>
      <button
        className="trace-event-head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="trace-event-index">{String(index + 1).padStart(2, "0")}</span>
        <span className="trace-event-name">{event.event}</span>
        {summary && <span className="trace-event-summary">{summary}</span>}
        <span className="trace-event-ts">{formatClockTime(event.ts)}</span>
        <span className={`trace-caret ${open ? "open" : ""}`}>›</span>
      </button>
      {open && (
        <div className="trace-event-body">
          <pre className="trace-code trace-code-tight">
            {prettyJson(omitShell(event))}
          </pre>
        </div>
      )}
    </li>
  );
}

// Pull a compact headline out of the event. Kept deliberately short — the
// full structure is one click away.
function eventSummary(event: TraceEvent): string {
  switch (event.event) {
    case "context_built": {
      const n = (event.working_context_page_ids as unknown[] | undefined)?.length ?? 0;
      const m = (event.preloaded_page_ids as unknown[] | undefined)?.length ?? 0;
      return `${n + m} pages in context`;
    }
    case "moves_executed": {
      const moves = (event.moves as unknown[] | undefined) ?? [];
      return `${moves.length} moves`;
    }
    case "llm_exchange": {
      const inTok = event.input_tokens as number | null | undefined;
      const outTok = event.output_tokens as number | null | undefined;
      const phase = event.phase as string | undefined;
      const parts: string[] = [];
      if (phase) parts.push(phase);
      if (inTok != null && outTok != null)
        parts.push(`${compactTokens(inTok)}→${compactTokens(outTok)}`);
      return parts.join(" · ");
    }
    case "dispatch_executed": {
      const t = event.child_call_type as string | undefined;
      const q = event.question_headline as string | undefined;
      return q ? `${t ?? "?"} → ${truncate(q, 60)}` : (t ?? "");
    }
    case "error":
    case "warning": {
      return truncate((event.message as string) ?? "", 80);
    }
    case "review_complete": {
      const fruit = event.remaining_fruit as number | null | undefined;
      return fruit != null ? `fruit ${fruit}` : "";
    }
    case "scoring_completed": {
      const sq = (event.subquestion_scores as unknown[] | undefined)?.length ?? 0;
      return sq > 0 ? `${sq} scored` : "";
    }
    case "load_page":
    case "explore_page":
    case "render_question_subgraph": {
      const id = event.page_id as string | undefined;
      const headline = event.page_headline as string | undefined;
      return headline ? truncate(headline, 60) : (id ?? "").slice(0, 8);
    }
    default:
      return "";
  }
}

function ExchangeList({ exchanges }: { exchanges: LLMExchangeSummary[] }) {
  return (
    <ul className="trace-exchange-list">
      {exchanges.map((x, i) => (
        <ExchangeRow key={x.id} summary={x} isFirst={i === 0} index={i} />
      ))}
    </ul>
  );
}

function ExchangeRow({
  summary,
  isFirst,
  index,
}: {
  summary: LLMExchangeSummary;
  isFirst: boolean;
  index: number;
}) {
  // First exchange opens by default so the reader lands on something
  // substantive. Later exchanges stay collapsed — the whole point of
  // TRACE mode is "on demand" access to heavy material.
  const [open, setOpen] = useState(isFirst);
  const [detail, setDetail] = useState<LLMExchangeDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open || detail) return;
    let cancelled = false;
    fetchLLMExchange(summary.id)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((e) => {
        if (!cancelled) setErr(e?.message ?? "failed");
      });
    return () => {
      cancelled = true;
    };
  }, [open, detail, summary.id]);

  return (
    <li className={`trace-exchange ${summary.error ? "has-error" : ""}`}>
      <button
        className="trace-exchange-head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="trace-exchange-index">
          {String(index + 1).padStart(2, "0")}
        </span>
        <span className="trace-exchange-phase">{summary.phase}</span>
        {summary.round != null && (
          <span className="trace-exchange-round">round {summary.round}</span>
        )}
        <span className="trace-exchange-tokens">
          {summary.input_tokens != null
            ? compactTokens(summary.input_tokens)
            : "?"}
          {"→"}
          {summary.output_tokens != null
            ? compactTokens(summary.output_tokens)
            : "?"}
        </span>
        {summary.duration_ms != null && (
          <span className="trace-exchange-dur">
            {formatMs(summary.duration_ms)}
          </span>
        )}
        {summary.error && <span className="trace-exchange-err-tag">err</span>}
        <span className={`trace-caret ${open ? "open" : ""}`}>›</span>
      </button>
      {open && (
        <div className="trace-exchange-body">
          {err && <div className="trace-detail-err">{err}</div>}
          {!detail && !err && (
            <div className="trace-detail-dim">loading exchange...</div>
          )}
          {detail && <ExchangeDetail detail={detail} />}
        </div>
      )}
    </li>
  );
}

function ExchangeDetail({ detail }: { detail: LLMExchangeDetail }) {
  // The three heavy panels are collapsed by default per the "on demand"
  // spirit. System prompt especially can be 25k+ chars. Messages are
  // visible by default because they're where a human actually starts
  // reading the conversation.
  return (
    <div className="trace-ex-detail">
      <Collapsible label="system prompt" defaultOpen={false}>
        {detail.system_prompt ? (
          <pre className="trace-code">{detail.system_prompt}</pre>
        ) : (
          <div className="trace-detail-dim">none</div>
        )}
      </Collapsible>

      <Collapsible
        label={`tool calls (${detail.tool_calls.length})`}
        defaultOpen={false}
      >
        {detail.tool_calls.length === 0 ? (
          <div className="trace-detail-dim">none</div>
        ) : (
          <div className="trace-ex-tools">
            {detail.tool_calls.map((t, i) => (
              <ToolCallBlock key={i} call={t} />
            ))}
          </div>
        )}
      </Collapsible>

      <div className="trace-ex-messages">
        <div className="trace-ex-section-label">messages</div>
        <MessageBlock role="user" content={detail.user_message} />
        <MessageBlock role="assistant" content={detail.response_text} />
        {detail.user_messages && detail.user_messages.length > 0 && (
          <Collapsible
            label={`conversation history (${detail.user_messages.length})`}
            defaultOpen={false}
          >
            <pre className="trace-code">
              {prettyJson(detail.user_messages)}
            </pre>
          </Collapsible>
        )}
      </div>

      <div className="trace-ex-foot">
        <span className="trace-ex-foot-label">usage</span>
        <span className="trace-ex-foot-value">
          {detail.input_tokens ?? "?"} in · {detail.output_tokens ?? "?"} out
          {detail.duration_ms != null &&
            ` · ${formatMs(detail.duration_ms)}`}
        </span>
      </div>
      {detail.error && (
        <div className="trace-detail-err">
          <strong>error:</strong> {detail.error}
        </div>
      )}
    </div>
  );
}

function ToolCallBlock({ call }: { call: { name: string; input: Record<string, unknown> | string } }) {
  // call.input is sometimes a dict, sometimes a stringified repr (see
  // backend: tool_calls are persisted as `{"name": ..., "input": str(...)}`
  // for legacy rows). Render both shapes gracefully.
  let rendered: string;
  if (typeof call.input === "string") {
    rendered = call.input;
  } else {
    rendered = prettyJson(call.input);
  }
  return (
    <div className="trace-ex-tool">
      <div className="trace-ex-tool-name">{call.name}</div>
      <pre className="trace-code trace-code-tight">{rendered}</pre>
    </div>
  );
}

function MessageBlock({
  role,
  content,
}: {
  role: "user" | "assistant";
  content: string | null | undefined;
}) {
  if (!content) return null;
  return (
    <div className={`trace-msg trace-msg-${role}`}>
      <div className="trace-msg-role">{role}</div>
      <div className="trace-msg-content">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
      </div>
    </div>
  );
}

function Collapsible({
  label,
  defaultOpen,
  children,
}: {
  label: string;
  defaultOpen: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="trace-coll">
      <button
        className="trace-coll-head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className={`trace-caret ${open ? "open" : ""}`}>›</span>
        <span className="trace-coll-label">{label}</span>
      </button>
      {open && <div className="trace-coll-body">{children}</div>}
    </div>
  );
}

function formatDuration(
  startIso: string,
  endIso: string | null,
): string | null {
  if (!endIso) return null;
  const ms = new Date(endIso).getTime() - new Date(startIso).getTime();
  if (ms < 0) return null;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
}

function formatMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatClockTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
}

function formatWhen(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function compactTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function compactJson(obj: unknown): string {
  try {
    const s = JSON.stringify(obj);
    return s.length > 80 ? s.slice(0, 77) + "..." : s;
  } catch {
    return "";
  }
}

function prettyJson(obj: unknown): string {
  try {
    return JSON.stringify(obj, null, 2);
  } catch {
    return String(obj);
  }
}

// Strip the trace-envelope shell (event, ts, call_id) when rendering the
// expanded event body — those are already shown in the head.
function omitShell(e: TraceEvent): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(e)) {
    if (k === "event" || k === "ts" || k === "call_id") continue;
    out[k] = v;
  }
  return out;
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "\u2026";
}
