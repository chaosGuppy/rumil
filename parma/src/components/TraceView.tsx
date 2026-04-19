"use client";

import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent,
  type ReactNode,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  commitRun,
  fetchCallEvents,
  fetchCallLLMExchanges,
  fetchLLMExchange,
  fetchPagesByIds,
  fetchRunSpend,
  fetchRunTraceTree,
  fetchProjectRuns,
  stageRun,
  startAbEval,
  updateRunHidden,
  type LLMExchangeDetail,
  type LLMExchangeSummary,
  type RunListItem,
  type RunSpend,
  type RunTraceTree,
  type TraceCallNode,
  type TraceEvent,
} from "@/lib/api";
import {
  classifyToolName,
  classifyValue,
  formatPreviewValue,
  KEY_HINTS,
  pickPreviewKey,
  shortenId,
  type KeyRenderHint,
} from "@/lib/tool-call-format";
import { ContextDiffPanel } from "./ContextDiffPanel";
import { useInspectPanel } from "./InspectPanelContext";
import { isPromoteEvent, UUID_OR_SHORT_RE } from "./NodeRefLink";

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
  // Called whenever the selected call changes (click in the tree, jump
  // from DISPATCHED BY, etc.). Parent can use this to keep the URL in
  // sync so a specific call is deep-linkable and survives navigation.
  onSelectedCallChange?: (callId: string | null) => void;
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
      onSelectedCallChange={props.onSelectedCallChange}
    />
  );
}

// Local storage for the RunPicker's show-hidden toggle. Per-project would
// be nicer but a single global knob keeps the mental model simple — when
// you're hunting for a smoke-test run you probably want them everywhere.
const SHOW_HIDDEN_RUNS_STORAGE_KEY = "parma:showHiddenRuns";
const RUN_PICKER_SORT_STORAGE_KEY = "parma:runPicker:sort";
const RUN_PICKER_FILTER_STORAGE_KEY = "parma:runPicker:filter";

type RunPickerSortMode = "recency" | "cost";

function loadShowHiddenRuns(): boolean {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(SHOW_HIDDEN_RUNS_STORAGE_KEY) === "1";
}

function loadRunPickerSort(): RunPickerSortMode {
  if (typeof window === "undefined") return "recency";
  const raw = window.localStorage.getItem(RUN_PICKER_SORT_STORAGE_KEY);
  return raw === "cost" ? "cost" : "recency";
}

function loadRunPickerFilter(): Set<string> {
  if (typeof window === "undefined") return new Set();
  const raw = window.localStorage.getItem(RUN_PICKER_FILTER_STORAGE_KEY);
  if (!raw) return new Set();
  try {
    const parsed: unknown = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      return new Set(parsed.filter((x): x is string => typeof x === "string"));
    }
  } catch {
    // corrupt value — fall through to empty set
  }
  return new Set();
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
  const [showHidden, setShowHidden] = useState(false);
  const [busyRunId, setBusyRunId] = useState<string | null>(null);
  // AB-eval multi-select. FIFO-eviction at 3rd selection keeps the
  // interaction stateless — no "clear" button needed, the user just keeps
  // picking runs. Order matters: first pick = A, second pick = B.
  const [selectedForAb, setSelectedForAb] = useState<string[]>([]);
  const [abLaunching, setAbLaunching] = useState(false);
  const [abError, setAbError] = useState<string | null>(null);
  // Filter + search + sort controls. Persisted to localStorage so the
  // chosen slice survives navigation — matches the showHidden pattern.
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [activeCallTypes, setActiveCallTypes] = useState<Set<string>>(
    () => new Set(),
  );
  const [sortMode, setSortMode] = useState<RunPickerSortMode>("recency");
  // Bumped whenever a lazily-resolved row lands, so the filter chip set
  // and any cost-sort re-evaluate against the synchronous cache.
  const [resolveTick, setResolveTick] = useState(0);

  useEffect(() => {
    setShowHidden(loadShowHiddenRuns());
    setSortMode(loadRunPickerSort());
    setActiveCallTypes(loadRunPickerFilter());
  }, []);

  useEffect(() => {
    const t = window.setTimeout(() => setDebouncedQuery(query), 100);
    return () => window.clearTimeout(t);
  }, [query]);

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

  const persistShowHidden = useCallback((next: boolean) => {
    setShowHidden(next);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(
        SHOW_HIDDEN_RUNS_STORAGE_KEY,
        next ? "1" : "0",
      );
    }
  }, []);

  const handleToggleHidden = useCallback(
    async (run: RunListItem) => {
      if (!run.run_id || busyRunId) return;
      const nextHidden = !run.hidden;
      setBusyRunId(run.run_id);
      try {
        await updateRunHidden(run.run_id, nextHidden);
        setRows((prev) => {
          if (!prev) return prev;
          return prev.map((r) =>
            r.run_id === run.run_id ? { ...r, hidden: nextHidden } : r,
          );
        });
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not update run");
      } finally {
        setBusyRunId(null);
      }
    },
    [busyRunId],
  );

  const handleToggleAbSelect = useCallback((runId: string) => {
    setAbError(null);
    setSelectedForAb((prev) => {
      if (prev.includes(runId)) {
        return prev.filter((id) => id !== runId);
      }
      // FIFO: when adding a third, drop the oldest so the user's latest two
      // picks are always the ones compared.
      const next = [...prev, runId];
      return next.length > 2 ? next.slice(next.length - 2) : next;
    });
  }, []);

  const handleLaunchAbEval = useCallback(async () => {
    if (selectedForAb.length !== 2 || abLaunching) return;
    setAbLaunching(true);
    setAbError(null);
    try {
      await startAbEval(selectedForAb[0], selectedForAb[1]);
      // Backend returns 202 before the eval finishes — the final ab_eval_report
      // id isn't known yet. Send the user to the list page so they can watch
      // it show up once the background task completes.
      if (typeof window !== "undefined") {
        window.location.href = "/ab-evals";
      }
    } catch (e) {
      setAbError(e instanceof Error ? e.message : "Could not start ab-eval");
      setAbLaunching(false);
    }
  }, [selectedForAb, abLaunching]);

  const persistSortMode = useCallback((next: RunPickerSortMode) => {
    setSortMode(next);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(RUN_PICKER_SORT_STORAGE_KEY, next);
    }
  }, []);

  const persistActiveCallTypes = useCallback(
    (updater: (prev: Set<string>) => Set<string>) => {
      setActiveCallTypes((prev) => {
        const next = updater(prev);
        if (typeof window !== "undefined") {
          window.localStorage.setItem(
            RUN_PICKER_FILTER_STORAGE_KEY,
            JSON.stringify([...next]),
          );
        }
        return next;
      });
    },
    [],
  );

  const handleToggleCallType = useCallback(
    (callType: string) => {
      persistActiveCallTypes((prev) => {
        const next = new Set(prev);
        if (next.has(callType)) next.delete(callType);
        else next.add(callType);
        return next;
      });
    },
    [persistActiveCallTypes],
  );

  const handleClearFilters = useCallback(() => {
    setQuery("");
    setDebouncedQuery("");
    persistActiveCallTypes(() => new Set());
  }, [persistActiveCallTypes]);

  // Prefetch resolutions for generic rows so the chip filter populates
  // with real call_types (instead of just the handful we know synchronously
  // from row.name). Also lets cost-sort work for resolved rows. Deduped by
  // the module-level cache — each run is fetched at most once per page load.
  useEffect(() => {
    if (!rows) return;
    let cancelled = false;
    for (const r of rows) {
      if (!r.run_id) continue;
      const baseName = r.name || "";
      const isGeneric = baseName === "" || baseName === "chat";
      if (!isGeneric) continue;
      if (runRowResolvedValues.has(r.run_id)) continue;
      resolveRunRow(r.run_id).then((resolved) => {
        if (cancelled || !resolved) return;
        setResolveTick((t) => t + 1);
      });
    }
    return () => {
      cancelled = true;
    };
  }, [rows]);

  if (error) return <div className="trace-pick-error">Failed to load runs: {error}</div>;
  if (!rows) return <div className="trace-pick-loading">Loading runs...</div>;

  const withRunId = rows.filter((r) => r.run_id);
  const visible = showHidden ? withRunId : withRunId.filter((r) => !r.hidden);
  const hiddenCount = withRunId.filter((r) => r.hidden).length;

  if (withRunId.length === 0) {
    return (
      <div className="trace-pick-empty">
        No runs in this project yet. Dispatch a call first.
      </div>
    );
  }

  // Distinct call_types among the currently-visible rows, resolved or
  // synchronously known. `resolveTick` triggers recomputation as lazy
  // resolutions land. Sorted alphabetically for stable chip order.
  void resolveTick;
  const availableCallTypes: string[] = [
    ...new Set(
      visible
        .map((r) => getRowCallType(r))
        .filter((x): x is string => x !== null),
    ),
  ].sort();

  let filtered: RunListItem[] = visible;
  if (debouncedQuery) {
    filtered = filtered.filter((r) => matchesRunQuery(r, debouncedQuery));
  }
  if (activeCallTypes.size > 0) {
    filtered = filtered.filter((r) => {
      const ct = getRowCallType(r);
      // Pass-through rows whose call_type we don't yet know — otherwise a
      // chip click would hide every lazy row until its resolve landed.
      if (ct === null) return true;
      return activeCallTypes.has(ct);
    });
  }
  if (sortMode === "cost") {
    // Resolved rows first, sorted by cost desc; unresolved rows tail in
    // original (recency) order so newly-arrived runs don't jump around.
    const withCost: Array<{ r: RunListItem; cost: number }> = [];
    const noCost: RunListItem[] = [];
    for (const r of filtered) {
      const c = getRowCostUsd(r);
      if (c !== null) withCost.push({ r, cost: c });
      else noCost.push(r);
    }
    withCost.sort((a, b) => b.cost - a.cost);
    filtered = [...withCost.map((x) => x.r), ...noCost];
  }

  const filterActive =
    debouncedQuery !== "" || activeCallTypes.size > 0;

  // Same-minute + same-name runs are indistinguishable by default. Group
  // them, compute config diffs across the group, and hand each row a
  // disambiguator (a list of distinguishing key:value chips, or a fallback
  // "#N of M" ordinal if configs are identical). Computed once per visible
  // list so rows just look up their own entry.
  const disambiguatorsByRunId = computeRunRowDisambiguators(filtered);

  return (
    <div className="trace-pick">
      <div className="trace-pick-head">
        <h2 className="trace-pick-title">Runs</h2>
        <p className="trace-pick-sub">
          Pick a run to inspect. Most-recent first.
        </p>
        {hiddenCount > 0 && (
          <label className="trace-pick-toggle">
            <input
              type="checkbox"
              checked={showHidden}
              onChange={(e) => persistShowHidden(e.target.checked)}
            />
            <span>
              show hidden
              <em className="trace-pick-toggle-hint">({hiddenCount})</em>
            </span>
          </label>
        )}
      </div>
      <div className="trace-pick-controls">
        <div className="trace-pick-controls-row trace-pick-controls-search">
          <input
            type="text"
            className="trace-pick-search"
            placeholder="filter by name, id, or call_type"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Filter runs"
          />
          <div className="trace-pick-sort" role="group" aria-label="Sort runs">
            <button
              type="button"
              className={
                "trace-pick-sort-btn "
                + (sortMode === "recency" ? "is-active" : "")
              }
              onClick={() => persistSortMode("recency")}
              title="Sort by most recent first"
            >
              recency
            </button>
            <button
              type="button"
              className={
                "trace-pick-sort-btn "
                + (sortMode === "cost" ? "is-active" : "")
              }
              onClick={() => persistSortMode("cost")}
              title="Sort by total run cost, highest first (resolved rows only)"
            >
              cost
            </button>
          </div>
        </div>
        {availableCallTypes.length > 0 && (
          <div className="trace-pick-controls-row trace-pick-chips">
            {availableCallTypes.map((ct) => {
              const active = activeCallTypes.has(ct);
              return (
                <button
                  key={ct}
                  type="button"
                  className={
                    "trace-pick-chip " + (active ? "is-active" : "")
                  }
                  onClick={() => handleToggleCallType(ct)}
                  aria-pressed={active}
                >
                  {ct}
                </button>
              );
            })}
            {filterActive && (
              <button
                type="button"
                className="trace-pick-chip trace-pick-chip-clear"
                onClick={handleClearFilters}
                title="Clear search and filter chips"
              >
                clear
              </button>
            )}
          </div>
        )}
      </div>
      {filtered.length === 0 ? (
        visible.length === 0 ? (
          <div className="trace-pick-empty-hidden">
            All runs in this project are hidden. Toggle &quot;show hidden&quot;
            to reveal them.
          </div>
        ) : (
          <div className="trace-pick-empty-hidden">
            No runs match the current filter.
            {" "}
            <button
              type="button"
              className="trace-pick-empty-clear"
              onClick={handleClearFilters}
            >
              clear filters
            </button>
          </div>
        )
      ) : (
        <div className="trace-pick-list">
          {filtered.map((r) => {
            const discriminators = extractRunDiscriminators(r.config);
            const busy = busyRunId === r.run_id;
            const abSlot = selectedForAb.indexOf(r.run_id!);
            const isAbSelected = abSlot !== -1;
            const rowClasses = [
              "trace-pick-row",
              r.hidden ? "is-hidden-run" : "",
              busy ? "is-busy" : "",
              isAbSelected ? "is-ab-selected" : "",
            ]
              .filter(Boolean)
              .join(" ");
            return (
              <div
                key={r.run_id!}
                role="button"
                tabIndex={0}
                className={rowClasses}
                onClick={() => onSelect(r.run_id!)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onSelect(r.run_id!);
                  }
                }}
              >
                <label
                  className="trace-pick-row-ab"
                  title={
                    isAbSelected
                      ? `Selected as ${abSlot === 0 ? "A" : "B"} for ab-eval`
                      : "Select for ab-eval"
                  }
                  onClick={(e) => e.stopPropagation()}
                  onKeyDown={(e) => e.stopPropagation()}
                  onMouseDown={(e) => e.stopPropagation()}
                >
                  <input
                    type="checkbox"
                    checked={isAbSelected}
                    onChange={() => handleToggleAbSelect(r.run_id!)}
                    aria-label={
                      isAbSelected ? "Deselect from ab-eval" : "Select for ab-eval"
                    }
                  />
                  {isAbSelected && (
                    <span className="trace-pick-row-ab-slot">
                      {abSlot === 0 ? "A" : "B"}
                    </span>
                  )}
                </label>
                <button
                  type="button"
                  className="trace-pick-row-hide"
                  onClick={(e) => {
                    e.stopPropagation();
                    e.preventDefault();
                    handleToggleHidden(r);
                  }}
                  onMouseDown={(e) => e.stopPropagation()}
                  onKeyDown={(e) => e.stopPropagation()}
                  disabled={busy}
                  title={r.hidden ? "Unhide run" : "Hide run"}
                  aria-label={r.hidden ? "Unhide run" : "Hide run"}
                >
                  {r.hidden ? "unhide" : "hide"}
                </button>
                <div className="trace-pick-row-head">
                  <RunRowLabel run={r} />
                  {discriminators
                    .filter((d) => d.key === "orchestrator")
                    .map((d) => (
                      <span
                        key={d.key}
                        className="trace-pick-row-orchestrator"
                        title={d.title}
                      >
                        {d.label}
                      </span>
                    ))}
                  {r.staged && <span className="trace-pick-row-staged">staged</span>}
                  {r.hidden && (
                    <span className="trace-pick-row-hidden">hidden</span>
                  )}
                </div>
                {r.question_summary && (
                  <div className="trace-pick-row-q">{r.question_summary}</div>
                )}
                <RunRowResolvedQuestion run={r} />
                <div className="trace-pick-row-meta">
                  <span>{r.run_id!.slice(0, 8)}</span>
                  <span>·</span>
                  <span>{formatWhen(r.created_at)}</span>
                  {discriminators
                    .filter((d) => d.key !== "orchestrator")
                    .map((d) => (
                      <span key={d.key} className="trace-pick-row-meta-chip" title={d.title}>
                        {d.label}
                      </span>
                    ))}
                  <RunRowResolvedModelChip run={r} />
                  {(disambiguatorsByRunId.get(r.run_id!) ?? []).map((d) => (
                    <span
                      key={`disamb-${d.key}`}
                      className="trace-pick-row-meta-chip trace-pick-row-disamb"
                      title={d.title}
                    >
                      {d.label}
                    </span>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
      {selectedForAb.length > 0 && (
        <div className="trace-pick-ab-bar" role="status">
          <div className="trace-pick-ab-bar-label">
            {selectedForAb.length === 1
              ? "Pick one more run to compare with ab-eval"
              : "Compare these two runs with ab-eval:"}
          </div>
          <div className="trace-pick-ab-bar-ids">
            {selectedForAb.map((id, i) => (
              <span key={id} className="trace-pick-ab-bar-id">
                <span className="trace-pick-ab-bar-slot">{i === 0 ? "A" : "B"}</span>
                <span className="trace-pick-ab-bar-short">{id.slice(0, 8)}</span>
              </span>
            ))}
          </div>
          <div className="trace-pick-ab-bar-actions">
            <button
              type="button"
              className="trace-pick-ab-bar-clear"
              onClick={() => {
                setSelectedForAb([]);
                setAbError(null);
              }}
              disabled={abLaunching}
            >
              clear
            </button>
            <button
              type="button"
              className="trace-pick-ab-bar-launch"
              onClick={handleLaunchAbEval}
              disabled={selectedForAb.length !== 2 || abLaunching}
            >
              {abLaunching ? "launching…" : "compare with ab-eval →"}
            </button>
          </div>
          {abError && <div className="trace-pick-ab-bar-err">{abError}</div>}
        </div>
      )}
    </div>
  );
}

// Resolved-from-tree details surfaced for generic runs ("chat",
// find_considerations, ...) so their rows read like the orchestrator rows
// (which already get name/question/model from run.config). We pull:
//  - call_type (from the root call in the tree)
//  - question headline (fall back to the root call's scope_page_summary
//    when the run has no question_id)
//  - model (from tree.config or the first llm_exchange event on the root)
// All optional — any fetch failure drops back to the plain name.
interface RunRowResolved {
  callType: string;
  question: string | null;
  model: string | null;
  costUsd: number | null;
}

// Module-level cache so RunRowLabel + RunRowResolvedQuestion +
// RunRowResolvedModelChip share one outstanding promise per run. Each row
// calls the hook up to 3 times; without this the picker would triple-fetch.
const runRowResolveCache = new Map<string, Promise<RunRowResolved | null>>();
// Synchronous mirror of the resolved values so the RunPicker's filter/sort
// logic can read them without awaiting. Populated as promises resolve.
const runRowResolvedValues = new Map<string, RunRowResolved>();

function resolveRunRow(runId: string): Promise<RunRowResolved | null> {
  const cached = runRowResolveCache.get(runId);
  if (cached) return cached;
  const promise = (async () => {
    try {
      const tree = await fetchRunTraceTree(runId);
      const root =
        tree.calls.find((c) => !c.call.parent_call_id) ?? tree.calls[0];
      if (!root) return null;
      const question =
        tree.question?.headline ?? root.scope_page_summary ?? null;
      const configModel =
        tree.config && typeof tree.config["model"] === "string"
          ? (tree.config["model"] as string)
          : null;
      let model = configModel;
      if (!model) {
        try {
          const events = await fetchCallEvents(root.call.id);
          model = modelFromEvents(events);
        } catch {
          // silent — model chip is nice-to-have
        }
      }
      const resolved: RunRowResolved = {
        callType: root.call.call_type,
        question,
        model,
        costUsd: tree.cost_usd,
      };
      runRowResolvedValues.set(runId, resolved);
      return resolved;
    } catch {
      return null;
    }
  })();
  runRowResolveCache.set(runId, promise);
  return promise;
}

// Best-effort synchronous call-type reader. Returns the resolved call_type
// if we've cached it, otherwise the base name if it's not the generic
// "chat"/empty placeholder, otherwise null — matches the behavior of the
// label component so filter chips line up with what the user sees.
function getRowCallType(run: RunListItem): string | null {
  if (run.run_id) {
    const resolved = runRowResolvedValues.get(run.run_id);
    if (resolved) return resolved.callType;
  }
  const name = run.name || "";
  if (!name || name === "chat") return null;
  return name;
}

function getRowCostUsd(run: RunListItem): number | null {
  if (!run.run_id) return null;
  return runRowResolvedValues.get(run.run_id)?.costUsd ?? null;
}

function matchesRunQuery(run: RunListItem, query: string): boolean {
  const q = query.toLowerCase();
  if (run.name && run.name.toLowerCase().includes(q)) return true;
  if (run.run_id && run.run_id.toLowerCase().includes(q)) return true;
  const callType = getRowCallType(run);
  if (callType && callType.toLowerCase().includes(q)) return true;
  return false;
}

// Shared lazy-fetch hook so RunRowLabel and RunRowExtras stay aligned on
// one outstanding request per row.
function useResolvedRunRow(run: RunListItem): RunRowResolved | null {
  const baseName = run.name || "";
  const isGeneric = baseName === "" || baseName === "chat";
  const [resolved, setResolved] = useState<RunRowResolved | null>(null);
  const runId = run.run_id;

  useEffect(() => {
    if (!isGeneric || !runId) return;
    let cancelled = false;
    resolveRunRow(runId).then((r) => {
      if (!cancelled && r) setResolved(r);
    });
    return () => {
      cancelled = true;
    };
  }, [isGeneric, runId]);

  return resolved;
}

function RunRowLabel({ run }: { run: RunListItem }) {
  const baseName = run.name || "";
  const isGeneric = baseName === "" || baseName === "chat";
  const resolved = useResolvedRunRow(run);
  const runId = run.run_id;

  const label =
    resolved?.callType ??
    (baseName || (runId ? runId.slice(0, 8) : "(unknown)"));
  const isSubstituted = isGeneric && resolved !== null;

  return (
    <span
      className={`trace-pick-row-name ${isSubstituted ? "is-resolved" : ""}`}
      title={
        isSubstituted && baseName
          ? `${baseName} — primary call: ${resolved?.callType}`
          : undefined
      }
    >
      {label}
    </span>
  );
}

// Surface resolved question (if the run had no question_summary) and model
// for generic runs. Parity with orchestrator rows — those already get a
// headline via run.question_summary and a model chip via
// extractRunDiscriminators(run.config).
function RunRowResolvedQuestion({ run }: { run: RunListItem }) {
  const resolved = useResolvedRunRow(run);
  if (!resolved?.question) return null;
  if (run.question_summary) return null;
  return <div className="trace-pick-row-q">{resolved.question}</div>;
}

function RunRowResolvedModelChip({ run }: { run: RunListItem }) {
  const resolved = useResolvedRunRow(run);
  // Only render when the base config didn't already supply a model chip —
  // otherwise extractRunDiscriminators() above is doing the job.
  const baseHasModel =
    run.config && typeof run.config["model"] === "string" && run.config["model"];
  if (!resolved?.model || baseHasModel) return null;
  return (
    <span
      className="trace-pick-row-meta-chip"
      title={`model: ${resolved.model}`}
    >
      {shortenModel(resolved.model)}
    </span>
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
  // capture_config() stores the factory-selected variant under
  // `prioritizer_variant`; older runs / custom entrypoints may use
  // `orchestrator`. Try both, prefer explicit `orchestrator`.
  const orchestratorRaw =
    (config["orchestrator"] as unknown) ?? (config["prioritizer_variant"] as unknown);
  if (typeof orchestratorRaw === "string" && orchestratorRaw) {
    out.push({
      key: "orchestrator",
      label: `orchestrator: ${orchestratorRaw}`,
      title: `orchestrator: ${orchestratorRaw}`,
    });
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
    out.push({
      key: "commit",
      label: `git ${commit.slice(0, 7)}`,
      title: `git_commit: ${commit}`,
    });
  }
  return out;
}

// Given a list of rows in display order, return a map from run_id to the
// disambiguating chips that should appear after the baseline discriminators.
// Rule: rows that share (minute, name) form a group. Within a group:
//  - If any config key takes more than one distinct value across the group,
//    emit one chip per differing key (label = "key=value").
//  - Otherwise fall back to a "#N / M" ordinal (stable by creation order).
// Group size 1 → no disambiguation.
function computeRunRowDisambiguators(
  rows: RunListItem[],
): Map<string, Array<{ key: string; label: string; title: string }>> {
  const out = new Map<string, Array<{ key: string; label: string; title: string }>>();
  const groups = new Map<string, RunListItem[]>();
  for (const r of rows) {
    if (!r.run_id) continue;
    const minute = r.created_at.slice(0, 16);
    const key = `${minute}|${r.name || ""}`;
    const list = groups.get(key) ?? [];
    list.push(r);
    groups.set(key, list);
  }
  for (const group of groups.values()) {
    if (group.length < 2) continue;
    // Collect all config keys that appear in any member.
    const keys = new Set<string>();
    for (const r of group) {
      for (const k of Object.keys(r.config ?? {})) keys.add(k);
    }
    const diffKeys: string[] = [];
    for (const k of keys) {
      const seen = new Set<string>();
      for (const r of group) {
        const v = (r.config ?? {})[k];
        seen.add(
          typeof v === "object" && v !== null ? JSON.stringify(v) : String(v),
        );
        if (seen.size > 1) break;
      }
      if (seen.size > 1) diffKeys.push(k);
    }
    // Sort oldest-first so the ordinal reads in creation order regardless
    // of how the parent sorted.
    const ordered = [...group].sort(
      (a, b) =>
        new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
    );
    ordered.forEach((r, idx) => {
      const chips: Array<{ key: string; label: string; title: string }> = [];
      if (diffKeys.length > 0) {
        for (const k of diffKeys) {
          const raw = (r.config ?? {})[k];
          const label =
            typeof raw === "object" && raw !== null
              ? `${k}=${truncate(JSON.stringify(raw), 24)}`
              : `${k}=${truncate(String(raw), 24)}`;
          chips.push({
            key: k,
            label,
            title: `config diff within this same-minute group: ${k}`,
          });
        }
      } else {
        chips.push({
          key: "ordinal",
          label: `#${idx + 1}/${ordered.length}`,
          title: (
            `#${idx + 1} of ${ordered.length} same-minute runs with the same `
            + "name and identical config"
          ),
        });
      }
      out.set(r.run_id!, chips);
    });
  }
  return out;
}

function TraceRunView({
  runId,
  projectId,
  initialCallId,
  onSelectedCallChange,
}: {
  runId: string;
  projectId: string;
  initialCallId?: string | null;
  onSelectedCallChange?: (callId: string | null) => void;
}) {
  const [tree, setTree] = useState<RunTraceTree | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [selectedCallId, setSelectedCallId] = useState<string | null>(
    initialCallId ?? null,
  );
  // Side-effects (router.replace inside onSelectedCallChange) cannot live in
  // a useState updater — React invokes updaters during render in concurrent
  // mode, which surfaces as "Cannot update Router while rendering". Sync via
  // an effect instead, and skip the initial mount + already-notified values
  // so we don't bounce the URL on first render or echo parent-driven updates.
  const lastNotifiedRef = useRef<string | null>(initialCallId ?? null);
  useEffect(() => {
    if (lastNotifiedRef.current === selectedCallId) return;
    lastNotifiedRef.current = selectedCallId;
    onSelectedCallChange?.(selectedCallId);
  }, [selectedCallId, onSelectedCallChange]);

  const refreshTree = useCallback(() => {
    setRefreshKey((k) => k + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    fetchRunTraceTree(runId)
      .then((t) => {
        if (cancelled) return;
        setTree(t);
        // Auto-select root call if nothing was pre-selected. Only run this
        // on the initial load — refreshTree() keeps the selected call.
        if (refreshKey === 0 && !initialCallId && t.calls.length > 0) {
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
  }, [runId, initialCallId, refreshKey]);

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
          onRefresh={refreshTree}
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
            onSelectCall={setSelectedCallId}
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
  onRefresh,
}: {
  tree: RunTraceTree;
  projectId: string;
  onRefresh: () => void;
}) {
  // Surface the most useful run-level context at the top: question
  // headline (if present), short run id, total cost, staged flag. This
  // replaces the cross-app jump — Brian never has to guess which run he's
  // looking at.
  const shortRunId = tree.run_id.slice(0, 8);
  const discriminators = extractRunDiscriminators(tree.config);
  return (
    <header className="trace-head">
      <div className="trace-head-row">
        <span className="trace-head-label">trace</span>
        <span className="trace-head-run-id" title={tree.run_id}>
          {shortRunId}
        </span>
        {tree.staged && <span className="trace-head-staged">staged</span>}
        <StageCommitToggle
          runId={tree.run_id}
          staged={tree.staged}
          onRefresh={onRefresh}
        />
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
      {discriminators.length > 0 && (
        <div className="trace-head-chips">
          {discriminators.map((d) => (
            <span
              key={d.key}
              className={`trace-head-chip trace-head-chip-${d.key}`}
              title={d.title}
            >
              {d.label}
            </span>
          ))}
        </div>
      )}
      <TraceSpendDetails runId={tree.run_id} />
      <TraceConfigDetails config={tree.config} />
      <div className="trace-head-project" title={projectId}>
        {projectId.slice(0, 8)} · project
      </div>
    </header>
  );
}

// Operator affordance: toggle a run between staged and baseline. When
// `staged` is true the button commits (with a confirm), otherwise it
// retroactively stages. On success we refetch the trace tree via
// onRefresh so the badge and other downstream UI update.
function StageCommitToggle({
  runId,
  staged,
  onRefresh,
}: {
  runId: string;
  staged: boolean;
  onRefresh: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleClick = useCallback(async () => {
    if (busy) return;
    if (staged) {
      // Commit is semi-irreversible — it flips staged=false and the run's
      // effects become visible to all readers. Confirm before firing.
      const ok = window.confirm(
        "Commit this staged run? Its effects will become visible to all "
        + "readers. You can't un-commit without retroactively re-staging "
        + "(which only works if no newer mutations depend on it).",
      );
      if (!ok) return;
    }
    setBusy(true);
    setError(null);
    try {
      if (staged) {
        await commitRun(runId);
      } else {
        await stageRun(runId);
      }
      onRefresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "action failed");
    } finally {
      setBusy(false);
    }
  }, [busy, staged, runId, onRefresh]);

  return (
    <>
      <button
        type="button"
        className="trace-head-stage-btn"
        onClick={handleClick}
        disabled={busy}
        title={
          staged
            ? "Commit this staged run (makes it visible to all readers)"
            : "Retroactively stage this run (hides its effects from other readers)"
        }
      >
        {busy ? "…" : staged ? "commit run" : "retroactively stage"}
      </button>
      {error && <span className="trace-head-stage-err">{error}</span>}
    </>
  );
}

function formatSpendDuration(ms: number): string {
  // Compact duration for the spend breakdown rows — sub-second: "230ms";
  // seconds: "4.2s"; minutes: "1m12s". Aggregations are always >0 when
  // shown (callers guard against the zero case).
  if (ms < 1000) return `${ms}ms`;
  const secs = ms / 1000;
  if (secs < 60) return `${secs.toFixed(1)}s`;
  const m = Math.floor(secs / 60);
  const s = Math.round(secs - m * 60);
  return s === 0 ? `${m}m` : `${m}m${s}s`;
}

function TraceSpendDetails({ runId }: { runId: string }) {
  const [open, setOpen] = useState(false);
  const [spend, setSpend] = useState<RunSpend | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Lazy-load the breakdown the first time the disclosure opens — the
    // default-collapsed state means we don't pay for it on header render,
    // which matters when flipping quickly between runs.
    if (!open || spend !== null || error !== null) return;
    let cancelled = false;
    fetchRunSpend(runId)
      .then((s) => {
        if (!cancelled) setSpend(s);
      })
      .catch((e) => {
        if (!cancelled) setError(e?.message ?? "failed to load spend");
      });
    return () => {
      cancelled = true;
    };
  }, [open, runId, spend, error]);

  const maxCost = useMemo(() => {
    if (!spend) return 0;
    return spend.by_call_type.reduce((m, r) => Math.max(m, r.cost_usd), 0);
  }, [spend]);

  return (
    <details
      className="trace-head-spend"
      open={open}
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
    >
      <summary className="trace-head-spend-summary">
        {open ? "hide" : "show"} spend breakdown
      </summary>
      <div className="trace-head-spend-body">
        {error && <div className="trace-head-spend-error">{error}</div>}
        {!error && !spend && (
          <div className="trace-head-spend-loading">loading…</div>
        )}
        {spend && spend.by_call_type.length === 0 && (
          <div className="trace-head-spend-empty">no calls recorded</div>
        )}
        {spend && spend.by_call_type.length > 0 && (
          <ul className="trace-head-spend-list">
            {spend.by_call_type.map((row) => {
              const pct =
                maxCost > 0 ? Math.max(2, (row.cost_usd / maxCost) * 100) : 0;
              return (
                <li key={row.call_type} className="trace-head-spend-row">
                  <div className="trace-head-spend-row-top">
                    <span className="trace-head-spend-label">
                      <span className="trace-head-spend-count">
                        {row.count}×
                      </span>
                      <span className="trace-head-spend-type">
                        {row.call_type}
                      </span>
                    </span>
                    <span className="trace-head-spend-values">
                      <span className="trace-head-spend-cost">
                        ${row.cost_usd.toFixed(4)}
                      </span>
                      {row.duration_ms > 0 && (
                        <span className="trace-head-spend-dur">
                          · {formatSpendDuration(row.duration_ms)}
                        </span>
                      )}
                    </span>
                  </div>
                  <div
                    className="trace-head-spend-bar"
                    style={{ width: `${pct}%` }}
                    aria-hidden
                  />
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </details>
  );
}

function TraceConfigDetails({
  config,
}: {
  config: Record<string, unknown> | null;
}) {
  const [open, setOpen] = useState(false);
  if (!config || Object.keys(config).length === 0) return null;
  const entryCount = Object.keys(config).length;
  const groups = groupConfigKeys(config);
  return (
    <details
      className="trace-head-config"
      open={open}
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
    >
      <summary className="trace-head-config-summary">
        {open ? "hide" : "show"} full config ({entryCount} fields)
      </summary>
      <div className="trace-head-config-body">
        {groups.map((g) => (
          <div key={g.name} className="trace-head-config-group">
            <div className="trace-head-config-group-title">{g.name}</div>
            <ToolCallArgs input={g.entries} />
          </div>
        ))}
      </div>
    </details>
  );
}

// Group a flat config dict into themed sections so 76-field dumps don't
// read as a wall. Themes are prefix- or keyword-based and intentionally
// coarse — anything that doesn't match a known theme falls into "other".
// Preserves the within-group insertion order via Object.fromEntries over
// entries filtered in definition order.
function groupConfigKeys(
  config: Record<string, unknown>,
): Array<{ name: string; entries: Record<string, unknown> }> {
  const rules: Array<{ name: string; match: (k: string) => boolean }> = [
    {
      name: "model",
      match: (k) => k === "model" || k.startsWith("model_") || k.startsWith("llm_"),
    },
    {
      name: "orchestrator",
      match: (k) =>
        k === "orchestrator"
        || k.startsWith("orchestrator_")
        || k === "prioritizer_variant"
        || k.startsWith("prioritizer_")
        || k.startsWith("two_phase_")
        || k.startsWith("claim_investigation_"),
    },
    {
      name: "available_moves",
      match: (k) => k === "available_moves" || k.startsWith("moves_"),
    },
    {
      name: "available_calls",
      match: (k) =>
        k === "assess_call_variant"
        || k.startsWith("call_")
        || k.startsWith("dispatch_")
        || k.endsWith("_call_variant"),
    },
    {
      name: "scoring",
      match: (k) =>
        k.startsWith("score_")
        || k.includes("scoring")
        || k.startsWith("credence_")
        || k.startsWith("importance_")
        || k.startsWith("robustness_"),
    },
    {
      name: "context",
      match: (k) =>
        k.startsWith("context_")
        || k.includes("char_budget")
        || k.includes("char_fraction")
        || k.startsWith("embedding_")
        || k.startsWith("full_page_")
        || k.startsWith("summary_page_")
        || k.startsWith("distillation_page_"),
    },
    {
      name: "budget",
      match: (k) => k === "budget" || k.startsWith("budget_") || k.includes("max_rounds"),
    },
    {
      name: "environment",
      match: (k) =>
        k === "git_commit"
        || k === "origin"
        || k === "workspace"
        || k === "project"
        || k === "smoke_test"
        || k.startsWith("env_"),
    },
  ];
  const out: Array<{ name: string; entries: Record<string, unknown> }> = [];
  const seen = new Set<string>();
  for (const rule of rules) {
    const entries: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(config)) {
      if (seen.has(k)) continue;
      if (rule.match(k)) {
        entries[k] = v;
        seen.add(k);
      }
    }
    if (Object.keys(entries).length > 0) {
      out.push({ name: rule.name, entries });
    }
  }
  const rest: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(config)) {
    if (!seen.has(k)) rest[k] = v;
  }
  if (Object.keys(rest).length > 0) {
    out.push({ name: "other", entries: rest });
  }
  return out;
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
  onSelectCall,
}: {
  callId: string;
  tree: RunTraceTree;
  onSelectCall: (id: string) => void;
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
  const callModel = modelFromEvents(events);
  const exchangeEventsById = useMemo(
    () => indexExchangeEvents(events),
    [events],
  );

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
          {callModel && (
            <span
              className="trace-detail-model"
              title={`model: ${callModel}`}
            >
              {shortenModel(callModel)}
            </span>
          )}
        </div>
        {node.scope_page_summary && (
          <h1 className="trace-detail-scope">{node.scope_page_summary}</h1>
        )}
        <dl className="trace-detail-meta">
          {call.parent_call_id && (
            <MetaRow
              label="dispatched by"
              value={
                <DispatchedByLink
                  parentCallId={call.parent_call_id}
                  tree={tree}
                  onSelectCall={onSelectCall}
                />
              }
            />
          )}
          {call.scope_page_id && (
            <MetaRow
              label="scope"
              value={<NodeRefChip pageId={call.scope_page_id} />}
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

      <ContextDiffPanel
        parentCallId={call.parent_call_id}
        callEvents={events}
      />

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
          <ExchangeList
            exchanges={exchanges}
            exchangeEventsById={exchangeEventsById}
          />
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

// Clickable short-id chip for any page reference. Click → inspect drawer,
// alt/cmd-click → pin as pane. Accepts a short id or a full UUID and
// always displays the 8-char short form.
function NodeRefChip({ pageId }: { pageId: string }) {
  const { openInspect, promoteToPane } = useInspectPanel();
  const shortId = shortenId(pageId);
  return (
    <button
      type="button"
      className="node-ref-link trace-detail-mono"
      onMouseDown={(e) => {
        if (isPromoteEvent(e)) e.preventDefault();
      }}
      onClick={(e) => {
        e.stopPropagation();
        if (isPromoteEvent(e)) {
          e.preventDefault();
          promoteToPane(shortId);
        } else {
          openInspect(shortId);
        }
      }}
      title={`Click to inspect · alt/cmd-click to pin as pane · ${shortId}`}
    >
      {shortId}
    </button>
  );
}

// A clickable short-id for the parent call. Switches the detail pane to
// the parent when clicked so sub-dispatches (scouts, claim-investigations)
// have an in-view backlink instead of relying on the sidebar indent alone.
// If the parent isn't in the loaded tree (shouldn't happen — all calls on
// a run share the tree), we still render the short id but the button is
// inert.
function DispatchedByLink({
  parentCallId,
  tree,
  onSelectCall,
}: {
  parentCallId: string;
  tree: RunTraceTree;
  onSelectCall: (id: string) => void;
}) {
  const parent = tree.calls.find((c) => c.call.id === parentCallId);
  const shortId = parentCallId.slice(0, 8);
  const typeHint = parent ? parent.call.call_type : "parent";
  return (
    <button
      type="button"
      className="trace-detail-parent-btn"
      onClick={() => onSelectCall(parentCallId)}
      disabled={!parent}
      title={
        parent
          ? `Jump to ${typeHint} ${shortId}`
          : `parent call ${shortId} not in this run's loaded tree`
      }
    >
      <span className="trace-detail-parent-type">{typeHint}</span>
      <span className="trace-detail-mono">{shortId}</span>
    </button>
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
      {open && <EventBody event={event} />}
    </li>
  );
}

function EventBody({ event }: { event: TraceEvent }) {
  if (event.event === "context_built") {
    return <ContextBuiltBody event={event} />;
  }
  return (
    <div className="trace-event-body">
      <JsonWithNodeRefs
        text={prettyJson(omitShell(event))}
        className="trace-code trace-code-tight"
      />
    </div>
  );
}

// Specialized body for context_built: tier breakdown + clickable page list
// with type + headline up top, raw JSON behind a toggle. The list is the
// primary view since "what was actually in this context" is the question
// the operator is trying to answer.
function ContextBuiltBody({ event }: { event: TraceEvent }) {
  const [showRaw, setShowRaw] = useState(false);
  const tiers = readPageIdTiers(event);
  const working = (event.working_context_page_ids as unknown[] | undefined) ?? [];
  const preloaded = (event.preloaded_page_ids as unknown[] | undefined) ?? [];
  const allIds = useMemo(
    () =>
      Array.from(
        new Set(
          [...working, ...preloaded].filter(
            (x): x is string => typeof x === "string",
          ),
        ),
      ),
    [working, preloaded],
  );
  const [pages, setPages] = useState<Record<string, FetchedPage> | null>(null);
  useEffect(() => {
    if (allIds.length === 0) {
      setPages({});
      return;
    }
    let cancelled = false;
    fetchPagesByIds(allIds)
      .then((p) => {
        if (!cancelled) setPages(p as Record<string, FetchedPage>);
      })
      .catch(() => {
        if (!cancelled) setPages({});
      });
    return () => {
      cancelled = true;
    };
  }, [allIds]);
  return (
    <div className="trace-event-body">
      {tiers ? (
        <ContextTierBreakdown tiers={tiers} pages={pages} />
      ) : (
        <ContextPageList ids={allIds} pages={pages} />
      )}
      <button
        type="button"
        className="trace-context-raw-toggle"
        onClick={() => setShowRaw((v) => !v)}
        aria-expanded={showRaw}
      >
        {showRaw ? "hide raw JSON" : "show raw JSON"}
      </button>
      {showRaw && (
        <JsonWithNodeRefs
          text={prettyJson(omitShell(event))}
          className="trace-code trace-code-tight"
        />
      )}
    </div>
  );
}

interface FetchedPage {
  id: string;
  page_type: string;
  headline: string;
}

function ContextPageList({
  ids,
  pages,
}: {
  ids: string[];
  pages: Record<string, FetchedPage> | null;
}) {
  if (ids.length === 0) {
    return <div className="trace-detail-dim">no pages in context</div>;
  }
  return (
    <div className="trace-context-page-list">
      {ids.map((id) => (
        <ContextPageRow key={id} id={id} page={pages?.[id]} />
      ))}
    </div>
  );
}

function ContextPageRow({
  id,
  page,
}: {
  id: string;
  page: FetchedPage | undefined;
}) {
  return (
    <div className="trace-context-page-row">
      <NodeRefChip pageId={id} />
      {page && (
        <span className={`trace-context-page-type node-${page.page_type}`}>
          {page.page_type}
        </span>
      )}
      {page && (
        <span className="trace-context-page-headline" title={page.headline}>
          {page.headline}
        </span>
      )}
    </div>
  );
}

// Context tier ordering — most-detailed first so readers see distillation
// (heaviest, most-load-bearing) before they scan down to summary.
const TIER_ORDER = ["distillation", "full", "abstract", "summary"] as const;
type TierName = (typeof TIER_ORDER)[number];

// Read the page_id_tiers map off a context_built event. Returns null when
// the field is missing (older trace_json rows) or empty so callers can skip
// the breakdown UI entirely. The map is page_id -> tier name; we invert it
// into tier -> [page_ids] for grouped rendering.
function readPageIdTiers(event: TraceEvent): Record<TierName, string[]> | null {
  const raw = event.page_id_tiers;
  if (!raw || typeof raw !== "object") return null;
  const grouped: Record<TierName, string[]> = {
    distillation: [],
    full: [],
    abstract: [],
    summary: [],
  };
  let any = false;
  for (const [pid, tier] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof tier !== "string") continue;
    if ((TIER_ORDER as readonly string[]).includes(tier)) {
      grouped[tier as TierName].push(pid);
      any = true;
    }
  }
  return any ? grouped : null;
}

// Render the tier counts as "12 full / 8 summary / 3 distillation". Used in
// the event summary line so researchers immediately see whether a claim was
// likely summarized away.
function formatTierCounts(tiers: Record<TierName, string[]>): string {
  const parts: string[] = [];
  for (const tier of TIER_ORDER) {
    const n = tiers[tier].length;
    if (n > 0) parts.push(`${n} ${tier}`);
  }
  return parts.join(" / ");
}

// Expanded-body component: groups page IDs by tier so a researcher can see
// exactly which pages were rendered at which detail level. Each row pairs
// the short id with page type + headline when the caller has resolved page
// metadata, so the operator doesn't have to click through to identify each.
function ContextTierBreakdown({
  tiers,
  pages,
}: {
  tiers: Record<TierName, string[]>;
  pages: Record<string, FetchedPage> | null;
}) {
  return (
    <div className="trace-context-tiers">
      {TIER_ORDER.map((tier) => {
        const ids = tiers[tier];
        if (ids.length === 0) return null;
        return (
          <div key={tier} className="trace-context-tier">
            <div className="trace-context-tier-label">
              {tier} ({ids.length})
            </div>
            <div className="trace-context-page-list">
              {ids.map((id) => (
                <ContextPageRow key={id} id={id} page={pages?.[id]} />
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// Pull a compact headline out of the event. Kept deliberately short — the
// full structure is one click away.
function eventSummary(event: TraceEvent): string {
  switch (event.event) {
    case "context_built": {
      const n = (event.working_context_page_ids as unknown[] | undefined)?.length ?? 0;
      const m = (event.preloaded_page_ids as unknown[] | undefined)?.length ?? 0;
      const tiers = readPageIdTiers(event);
      const breakdown = tiers ? formatTierCounts(tiers) : "";
      const base = `${n + m} pages in context`;
      return breakdown ? `${base} · ${breakdown}` : base;
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

function ExchangeList({
  exchanges,
  exchangeEventsById,
}: {
  exchanges: LLMExchangeSummary[];
  exchangeEventsById: Record<string, TraceEvent>;
}) {
  // Surface an inline "ROUND N" caption whenever the round changes so
  // multi-round scout loops visually group instead of reading as a flat
  // 30-row list. Null rounds (single-pass calls) get no caption — the
  // list is one block and the header already conveys that.
  return (
    <ul className="trace-exchange-list">
      {exchanges.map((x, i) => {
        const prev = i > 0 ? exchanges[i - 1] : null;
        const showDivider =
          x.round != null && (prev == null || prev.round !== x.round);
        return (
          <Fragment key={x.id}>
            {showDivider && (
              <li
                className="trace-exchange-round-divider"
                aria-hidden
              >
                ROUND {x.round}
              </li>
            )}
            <ExchangeRow
              summary={x}
              isFirst={i === 0}
              index={i}
              event={exchangeEventsById[x.id]}
            />
          </Fragment>
        );
      })}
    </ul>
  );
}

function ExchangeRow({
  summary,
  isFirst,
  index,
  event,
}: {
  summary: LLMExchangeSummary;
  isFirst: boolean;
  index: number;
  event: TraceEvent | undefined;
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

  const model =
    event && typeof event.model === "string" ? (event.model as string) : null;
  const cacheRead =
    event && typeof event.cache_read_input_tokens === "number"
      ? (event.cache_read_input_tokens as number)
      : null;
  const cacheCreate =
    event && typeof event.cache_creation_input_tokens === "number"
      ? (event.cache_creation_input_tokens as number)
      : null;

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
        {model && (
          <span className="trace-exchange-model" title={`model: ${model}`}>
            {shortenModel(model)}
          </span>
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
          {detail && (
            <ExchangeDetail
              detail={detail}
              model={model}
              cacheRead={cacheRead}
              cacheCreate={cacheCreate}
            />
          )}
        </div>
      )}
    </li>
  );
}

function ExchangeDetail({
  detail,
  model,
  cacheRead,
  cacheCreate,
}: {
  detail: LLMExchangeDetail;
  model: string | null;
  cacheRead: number | null;
  cacheCreate: number | null;
}) {
  // The three heavy panels are collapsed by default per the "on demand"
  // spirit. System prompt especially can be 25k+ chars. Messages are
  // visible by default because they're where a human actually starts
  // reading the conversation.
  //
  // The top header shows the model (if recorded in the trace event) and a
  // compact usage line with cache breakdown — the two pieces of info a
  // trace-debugger usually wants at a glance before expanding panels.
  const messagesForCopy = useMemo(() => {
    const parts: string[] = [];
    if (detail.user_message) {
      parts.push(`# user\n\n${detail.user_message}`);
    }
    if (detail.response_text) {
      parts.push(`# assistant\n\n${detail.response_text}`);
    }
    if (detail.user_messages && detail.user_messages.length > 0) {
      parts.push(
        "# conversation history\n\n" + prettyJson(detail.user_messages),
      );
    }
    return parts.join("\n\n---\n\n");
  }, [detail]);

  return (
    <div className="trace-ex-detail">
      <div className="trace-ex-header-meta">
        {model && (
          <span
            className="trace-ex-header-model"
            title={`model: ${model}`}
          >
            {shortenModel(model)}
          </span>
        )}
        <span className="trace-ex-header-usage">
          in: {formatTokenCount(detail.input_tokens)}
          {cacheRead != null && cacheRead > 0 && (
            <>
              {" "}
              <span
                className="trace-ex-header-cache"
                title={
                  "Cache-read tokens (input tokens served from an "
                  + "existing cache-control block — ~90% discount vs "
                  + "uncached input). Anthropic field: "
                  + "cache_read_input_tokens."
                }
              >
                (cache: {formatTokenCount(cacheRead)})
              </span>
            </>
          )}
          {cacheCreate != null && cacheCreate > 0 && (
            <>
              {" "}
              <span
                className="trace-ex-header-cache"
                title={
                  "Cache-write tokens (new cache-control blocks written "
                  + "this turn — ~25% premium over uncached input, but "
                  + "subsequent turns pay the cache-read rate). "
                  + "Anthropic field: cache_creation_input_tokens."
                }
              >
                (cache-w: {formatTokenCount(cacheCreate)})
              </span>
            </>
          )}
          {" · out: "}
          {formatTokenCount(detail.output_tokens)}
          {detail.duration_ms != null &&
            ` · ${formatMs(detail.duration_ms)}`}
        </span>
      </div>

      <div className="trace-ex-sec">
        <div className="trace-ex-sec-head">
          <div className="trace-ex-sec-title">SYSTEM PROMPT</div>
          {detail.system_prompt && (
            <CopyButton text={detail.system_prompt} label="copy prompt" />
          )}
        </div>
        <Collapsible label="show system prompt" defaultOpen={false}>
          {detail.system_prompt ? (
            <pre className="trace-code">{detail.system_prompt}</pre>
          ) : (
            <div className="trace-detail-dim">none</div>
          )}
        </Collapsible>
      </div>

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
        <div className="trace-ex-sec-head">
          <div className="trace-ex-sec-title">MESSAGES</div>
          {messagesForCopy && (
            <CopyButton text={messagesForCopy} label="copy messages" />
          )}
        </div>
        <MessageBlock role="user" content={detail.user_message} />
        <MessageBlock role="assistant" content={detail.response_text} />
        {detail.user_messages && detail.user_messages.length > 0 && (
          <Collapsible
            label={`conversation history (${detail.user_messages.length})`}
            defaultOpen={false}
          >
            <JsonWithNodeRefs
              text={prettyJson(detail.user_messages)}
              className="trace-code"
            />
          </Collapsible>
        )}
      </div>

      {detail.error && (
        <div className="trace-detail-err">
          <strong>error:</strong> {detail.error}
        </div>
      )}
    </div>
  );
}

function formatTokenCount(n: number | null | undefined): string {
  if (n == null) return "?";
  return compactTokens(n);
}

function ToolCallBlock({
  call,
}: {
  call: { name: string; input: Record<string, unknown> | string };
}) {
  // call.input is usually a dict, but legacy rows persist it as a
  // stringified repr (see backend: `tool_calls` serialization). The string
  // shape can't be usefully structured, so it always falls back to raw.
  const inputIsDict = typeof call.input !== "string";
  const [expanded, setExpanded] = useState(false);
  const [rawMode, setRawMode] = useState(!inputIsDict);

  const family = classifyToolName(call.name);
  const inputDict = inputIsDict
    ? (call.input as Record<string, unknown>)
    : null;
  // Collect every page-ref arg so the preview can surface them inline.
  // Reviewers skimming for "which pages did this tool touch" shouldn't have
  // to expand each call one at a time.
  const refEntries: Array<{ key: string; value: string }> = [];
  if (inputDict) {
    for (const [k, v] of Object.entries(inputDict)) {
      if (KEY_HINTS[k] === "ref" && typeof v === "string") {
        refEntries.push({ key: k, value: v });
      } else if (Array.isArray(v) && v.every((x) => typeof x === "string")) {
        // page_ids: ["..."] style — only surface if the key itself looks
        // like a ref collection (ends with `_ids`).
        if (/_ids$/.test(k)) {
          for (const id of v as string[]) refEntries.push({ key: k, value: id });
        }
      }
    }
  }
  const previewKey = inputDict && refEntries.length === 0
    ? pickPreviewKey(inputDict)
    : null;
  const previewValue =
    inputDict && previewKey ? inputDict[previewKey] : undefined;
  const extraKeys = inputDict
    ? Object.keys(inputDict).filter(
        (k) => !refEntries.some((e) => e.key === k) && k !== previewKey,
      )
    : [];

  const toggleExpanded = useCallback(() => setExpanded((v) => !v), []);
  const toggleRaw = useCallback((e: MouseEvent) => {
    e.stopPropagation();
    setRawMode((v) => !v);
    setExpanded(true);
  }, []);

  return (
    <div className={`tc-block tc-family-${family}`}>
      <div className="tc-head-row">
        <button
          type="button"
          className="tc-head"
          onClick={toggleExpanded}
          aria-expanded={expanded}
        >
          <span className={`tc-caret ${expanded ? "open" : ""}`}>›</span>
          <span className="tc-name">{call.name}</span>
          {!expanded && refEntries.length > 0 && (
            <span className="tc-preview">
              <span className="tc-preview-brace">{"{ "}</span>
              {refEntries.map((e, i) => (
                <Fragment key={`${e.key}-${i}`}>
                  {i > 0 && <span className="tc-preview-sep">, </span>}
                  <span className="tc-preview-key">{e.key}</span>
                  <span className="tc-preview-colon">: </span>
                  <span className="tc-preview-value">
                    <NodeRefChip pageId={e.value} />
                  </span>
                </Fragment>
              ))}
              {extraKeys.length > 0 && (
                <span
                  className="tc-preview-more"
                  title={
                    `${extraKeys.length} other argument key${extraKeys.length > 1 ? "s" : ""}: `
                    + extraKeys.join(", ")
                  }
                >
                  {", +"}
                  {extraKeys.length}
                </span>
              )}
              <span className="tc-preview-brace">{" }"}</span>
            </span>
          )}
          {!expanded && refEntries.length === 0 && previewKey && (
            <span className="tc-preview">
              <span className="tc-preview-brace">{"{ "}</span>
              <span className="tc-preview-key">{previewKey}</span>
              <span className="tc-preview-colon">: </span>
              <span className="tc-preview-value">
                {formatPreviewValue(previewValue)}
              </span>
              {extraKeys.length > 0 && (
                <span
                  className="tc-preview-more"
                  title={
                    `+${extraKeys.length}`
                    + " more argument keys on this tool call (expand to see them)"
                  }
                >
                  {", +"}
                  {extraKeys.length}
                </span>
              )}
              <span className="tc-preview-brace">{" }"}</span>
            </span>
          )}
          {!expanded && !previewKey && refEntries.length === 0 && inputIsDict && (
            <span className="tc-preview tc-preview-empty">{"{ }"}</span>
          )}
        </button>
        {inputIsDict && (
          <button
            type="button"
            className={`tc-raw-toggle ${rawMode ? "active" : ""}`}
            onClick={toggleRaw}
            title={rawMode ? "Switch to formatted view" : "Switch to raw JSON"}
          >
            raw
          </button>
        )}
      </div>
      {expanded && (
        <div className="tc-body">
          {rawMode || !inputIsDict ? (
            <JsonWithNodeRefs
              text={
                typeof call.input === "string"
                  ? call.input
                  : prettyJson(call.input)
              }
              className="trace-code trace-code-tight"
            />
          ) : (
            <ToolCallArgs input={call.input as Record<string, unknown>} />
          )}
        </div>
      )}
    </div>
  );
}

// Render a tool-call input dict as labeled rows. Keys get render hints
// from KEY_HINTS; unknown keys fall back to JSON stringify.
function ToolCallArgs({ input }: { input: Record<string, unknown> }) {
  const entries = Object.entries(input);
  if (entries.length === 0) {
    return <div className="tc-args-empty">(no arguments)</div>;
  }
  return (
    <div className="tc-args">
      {entries.map(([key, value]) => (
        <ToolCallArgRow key={key} argKey={key} value={value} />
      ))}
    </div>
  );
}

function ToolCallArgRow({
  argKey,
  value,
}: {
  argKey: string;
  value: unknown;
}) {
  const keyHint: KeyRenderHint = KEY_HINTS[argKey] ?? "default";
  const valueClass = classifyValue(value);
  // Effective rendering picks the strictest interpretation: a key hinted as
  // "ref" overrides content heuristics; a key hinted as "longtext" wins
  // over default; otherwise defer to the value-based classification.
  const effective: KeyRenderHint =
    keyHint === "ref" || keyHint === "longtext" || keyHint === "pill" || keyHint === "muted"
      ? keyHint
      : valueClass === "ref"
        ? "ref"
        : valueClass === "longtext"
          ? "longtext"
          : "default";

  return (
    <div className={`tc-row tc-row-${effective}`}>
      <div className="tc-row-key">{argKey}</div>
      <div className="tc-row-value">
        <ToolCallArgValue hint={effective} value={value} />
      </div>
    </div>
  );
}

function ToolCallArgValue({
  hint,
  value,
}: {
  hint: KeyRenderHint;
  value: unknown;
}) {
  const { openInspect, promoteToPane } = useInspectPanel();

  if (hint === "ref" && typeof value === "string") {
    const shortId = shortenId(value);
    return (
      <button
        type="button"
        className="node-ref-link"
        onMouseDown={(e) => {
          if (isPromoteEvent(e)) e.preventDefault();
        }}
        onClick={(e) => {
          if (isPromoteEvent(e)) {
            e.preventDefault();
            promoteToPane(shortId);
          } else {
            openInspect(shortId);
          }
        }}
        title={`Click to inspect · alt/cmd-click to pin as pane · ${shortId}`}
      >
        {shortId}
      </button>
    );
  }

  if (hint === "longtext" && typeof value === "string") {
    return <LongTextValue text={value} />;
  }

  if (hint === "pill" && (typeof value === "number" || typeof value === "string")) {
    return <span className="tc-pill">{String(value)}</span>;
  }

  if (hint === "muted") {
    return <span className="tc-muted">{String(value)}</span>;
  }

  // Nested objects recurse through ToolCallArgs so inner fields get the
  // same hint-based styling (refs become clickable, longtext preserves
  // newlines, etc.) instead of being flattened to a preview string.
  // Arrays of primitives render inline; arrays of objects render as a
  // stack of indented sub-blocks with item numbers.
  if (value && typeof value === "object") {
    if (Array.isArray(value)) {
      if (value.length === 0) {
        return <span className="tc-muted">[]</span>;
      }
      const allPrimitive = value.every(
        (v) => v === null || ["string", "number", "boolean"].includes(typeof v),
      );
      if (allPrimitive) {
        return (
          <div className="tc-array">
            {value.map((item, i) => (
              <span key={i} className="tc-array-item">
                {typeof item === "string" &&
                (item.length > 200 || item.includes("\n")) ? (
                  <LongTextValue text={item} />
                ) : (
                  String(item)
                )}
              </span>
            ))}
          </div>
        );
      }
      return (
        <div className="tc-array-objects">
          {value.map((item, i) => (
            <div key={i} className="tc-array-item-card">
              <div className="tc-array-item-index">#{i + 1}</div>
              {item && typeof item === "object" && !Array.isArray(item) ? (
                <ToolCallArgs input={item as Record<string, unknown>} />
              ) : (
                <ToolCallArgValue hint="default" value={item} />
              )}
            </div>
          ))}
        </div>
      );
    }
    const nested = value as Record<string, unknown>;
    if (Object.keys(nested).length === 0) {
      return <span className="tc-muted">{"{}"}</span>;
    }
    return (
      <div className="tc-nested">
        <ToolCallArgs input={nested} />
      </div>
    );
  }

  if (typeof value === "string") {
    return (
      <span className="tc-string">
        <InlineTextWithNodeRefs text={value} />
      </span>
    );
  }
  return <span className="tc-scalar">{String(value)}</span>;
}

// Inline variant of TextWithNodeRefs that doesn't require an onNodeRef
// prop — it pulls openInspect / promoteToPane directly from the inspect
// panel context. Used wherever tool-call args render a plain string that
// might contain embedded 8-hex short-ids.
function InlineTextWithNodeRefs({ text }: { text: string }) {
  const { openInspect, promoteToPane } = useInspectPanel();
  const parts: ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  const re = new RegExp(UUID_OR_SHORT_RE);
  while ((match = re.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    const shortId = match[1];
    const matched = match[0];
    parts.push(
      <button
        key={`${match.index}-${shortId}`}
        type="button"
        className="node-ref-link"
        onMouseDown={(e) => {
          if (isPromoteEvent(e)) e.preventDefault();
        }}
        onClick={(e) => {
          e.stopPropagation();
          if (isPromoteEvent(e)) {
            e.preventDefault();
            promoteToPane(shortId);
          } else {
            openInspect(shortId);
          }
        }}
        title={`Click to inspect · alt/cmd-click to pin as pane · ${shortId}`}
      >
        {matched}
      </button>,
    );
    lastIndex = re.lastIndex;
  }
  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }
  if (parts.length === 0) return <>{text}</>;
  return <>{parts}</>;
}

// Long-text renderer with pre-wrap and optional "show more" above 2000
// chars. Preserves newlines/indentation so prose reads naturally instead
// of appearing as an escaped JSON blob.
function LongTextValue({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const LIMIT = 2000;
  const tooLong = text.length > LIMIT;
  const shown = !tooLong || expanded ? text : text.slice(0, LIMIT) + "\u2026";
  return (
    <div className="tc-longtext-wrap">
      <div className="tc-longtext">
        <InlineTextWithNodeRefs text={shown} />
      </div>
      {tooLong && (
        <button
          type="button"
          className="tc-longtext-toggle"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded
            ? "show less"
            : `show more (${text.length - LIMIT} more chars)`}
        </button>
      )}
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

// Trim the familiar "claude-" prefix and the trailing -YYYYMMDD date so the
// chip fits on one line — e.g. "claude-sonnet-4-6-20251201" -> "sonnet-4-6".
// The full name is preserved in the element's title attribute.
function shortenModel(model: string): string {
  return model.replace(/^claude-/, "").replace(/-\d{8}$/, "");
}

// Pull the model name out of a call's trace events. Uses the first
// llm_exchange event (calls usually share a model across exchanges). Returns
// null when no model is recorded (pre-model-event legacy traces).
function modelFromEvents(events: TraceEvent[] | null): string | null {
  if (!events) return null;
  for (const e of events) {
    if (e.event === "llm_exchange") {
      const m = e.model;
      if (typeof m === "string" && m) return m;
    }
  }
  return null;
}

// Lookup table from exchange_id to the llm_exchange trace event for that
// exchange, so the detail view can show per-exchange model and cache tokens
// without another round trip.
function indexExchangeEvents(
  events: TraceEvent[] | null,
): Record<string, TraceEvent> {
  if (!events) return {};
  const out: Record<string, TraceEvent> = {};
  for (const e of events) {
    if (e.event !== "llm_exchange") continue;
    const id = e.exchange_id;
    if (typeof id === "string") out[id] = e;
  }
  return out;
}

// Small button that copies `text` to the clipboard and flashes "copied" for
// a beat so the user gets visible confirmation. Absorbs the click so parent
// collapsibles don't toggle. Kept minimal — no icons, no deps.
function CopyButton({ text, label = "copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const handle = useCallback(
    (e: MouseEvent) => {
      e.stopPropagation();
      e.preventDefault();
      if (typeof navigator === "undefined" || !navigator.clipboard) return;
      navigator.clipboard
        .writeText(text)
        .then(() => {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1200);
        })
        .catch(() => {
          // Clipboard can reject in insecure contexts; stay silent rather
          // than surface a nag.
        });
    },
    [text],
  );
  return (
    <button
      type="button"
      className={`trace-copy-btn ${copied ? "is-copied" : ""}`}
      onClick={handle}
      title={`Copy ${label} to clipboard`}
      aria-label={`Copy ${label}`}
    >
      {copied ? "copied" : label}
    </button>
  );
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

// Render a text blob (usually pretty-printed JSON) with any 8-char hex
// short-ids wrapped as clickable NodeRefLink buttons. Click → openInspect,
// alt/cmd-click → promoteToPane. Mirrors the pattern in NodeRefLink.tsx's
// TextWithNodeRefs but renders inside a <pre> so whitespace/indent survives.
function JsonWithNodeRefs({
  text,
  className,
}: {
  text: string;
  className?: string;
}) {
  const { openInspect, promoteToPane } = useInspectPanel();
  const parts: ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  const re = new RegExp(UUID_OR_SHORT_RE);
  while ((match = re.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    const shortId = match[1];
    const matched = match[0];
    parts.push(
      <button
        key={`${match.index}-${shortId}`}
        type="button"
        className="node-ref-link"
        onMouseDown={(e) => {
          if (isPromoteEvent(e)) e.preventDefault();
        }}
        onClick={(e) => {
          e.stopPropagation();
          if (isPromoteEvent(e)) {
            e.preventDefault();
            promoteToPane(shortId);
          } else {
            openInspect(shortId);
          }
        }}
        title={`Click to inspect · alt/cmd-click to pin as pane · ${shortId}`}
      >
        {matched}
      </button>,
    );
    lastIndex = re.lastIndex;
  }
  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }
  return <pre className={className}>{parts}</pre>;
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
