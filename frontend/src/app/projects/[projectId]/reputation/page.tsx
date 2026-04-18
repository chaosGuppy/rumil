"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import type {
  Project,
  ReputationBucketOut,
  ReputationEvent,
  ReputationSummaryOut,
} from "@/api";

import { CLIENT_API_BASE as API_BASE } from "@/api-config";
import { WorkspaceIndicator } from "@/components/workspace-indicator";
import { useDocumentTitle } from "@/lib/use-document-title";

type BucketsBySource = Record<string, ReputationBucketOut[]>;

function groupBySource(buckets: ReputationBucketOut[]): BucketsBySource {
  const out: BucketsBySource = {};
  for (const b of buckets) {
    (out[b.source] ??= []).push(b);
  }
  return out;
}

function sourceUnitsHint(source: string): string {
  if (source === "eval_agent") return "preference score (-3..+3)";
  if (source === "human_feedback") return "flag weight (1.0 per flag)";
  return "native units";
}

function formatScore(n: number): string {
  if (Number.isInteger(n)) return n.toFixed(1);
  return n.toFixed(2);
}

function formatTs(ts: string): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export default function ReputationPage() {
  const params = useParams<{ projectId: string }>();
  const projectId = params.projectId;

  const [projectName, setProjectName] = useState<string>();
  const [summary, setSummary] = useState<ReputationSummaryOut | null>(null);
  const [events, setEvents] = useState<ReputationEvent[]>([]);
  const [qcFindings, setQcFindings] = useState<ReputationEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [orchestrator, setOrchestrator] = useState<string>("");
  const [source, setSource] = useState<string>("");
  const [dimension, setDimension] = useState<string>("");

  useDocumentTitle(projectName ? `${projectName} \u00b7 reputation` : null);

  useEffect(() => {
    fetch(`${API_BASE}/api/projects/${projectId}`, { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null))
      .then((d: Project | null) => {
        if (d) setProjectName(d.name);
      });
  }, [projectId]);

  useEffect(() => {
    setLoading(true);
    setError(null);

    const qs = new URLSearchParams();
    if (orchestrator) qs.set("orchestrator", orchestrator);
    if (source) qs.set("source", source);
    if (dimension) qs.set("dimension", dimension);
    const qsString = qs.toString();
    const summaryUrl = `${API_BASE}/api/projects/${projectId}/reputation${qsString ? `?${qsString}` : ""}`;

    const eventsQs = new URLSearchParams(qs);
    eventsQs.set("limit", "20");
    const eventsUrl = `${API_BASE}/api/projects/${projectId}/reputation/events?${eventsQs.toString()}`;

    // Separate fetch for QC findings so the "Quality issues" panel always
    // reflects the full QC picture for the active filter set, not just the
    // most recent 20 mixed events. Limit is generous but bounded.
    const qcQs = new URLSearchParams();
    if (orchestrator) qcQs.set("orchestrator", orchestrator);
    qcQs.set("source", "eval_agent");
    qcQs.set("dimension", "quality_control");
    qcQs.set("limit", "200");
    const qcUrl = `${API_BASE}/api/projects/${projectId}/reputation/events?${qcQs.toString()}`;

    Promise.all([
      fetch(summaryUrl, { cache: "no-store" }).then(async (r) => {
        if (!r.ok) throw new Error(`summary ${r.status}`);
        return (await r.json()) as ReputationSummaryOut;
      }),
      fetch(eventsUrl, { cache: "no-store" }).then(async (r) => {
        if (!r.ok) throw new Error(`events ${r.status}`);
        return (await r.json()) as ReputationEvent[];
      }),
      fetch(qcUrl, { cache: "no-store" }).then(async (r) => {
        if (!r.ok) throw new Error(`qc ${r.status}`);
        return (await r.json()) as ReputationEvent[];
      }),
    ])
      .then(([s, e, qc]) => {
        setSummary(s);
        setEvents(e);
        setQcFindings(qc.filter((ev) => ev.score < 0));
        setLoading(false);
      })
      .catch((e) => {
        setError(String(e));
        setLoading(false);
      });
  }, [projectId, orchestrator, source, dimension]);

  // Derive filter options from the currently visible data plus current
  // selections (so a selected value is always an option, even if the
  // current filtered set would otherwise hide it).
  const { orchestratorOptions, sourceOptions, dimensionOptions } = useMemo(() => {
    const orchs = new Set<string>();
    const srcs = new Set<string>();
    const dims = new Set<string>();
    for (const b of summary?.buckets ?? []) {
      if (b.orchestrator) orchs.add(b.orchestrator);
      srcs.add(b.source);
      dims.add(b.dimension);
    }
    if (orchestrator) orchs.add(orchestrator);
    if (source) srcs.add(source);
    if (dimension) dims.add(dimension);
    return {
      orchestratorOptions: [...orchs].sort(),
      sourceOptions: [...srcs].sort(),
      dimensionOptions: [...dims].sort(),
    };
  }, [summary, orchestrator, source, dimension]);

  const bySource = useMemo(
    () => groupBySource(summary?.buckets ?? []),
    [summary],
  );

  const hasFilters = !!(orchestrator || source || dimension);

  return (
    <main className="reputation-page">
      <style>{`
        .reputation-page {
          max-width: 72rem;
          margin: 0 auto;
          padding: 2rem;
          font-family: var(--font-geist-sans), system-ui, sans-serif;
        }

        .rep-header {
          margin-top: 1.5rem;
          margin-bottom: 2rem;
          display: flex;
          align-items: baseline;
          justify-content: space-between;
          gap: 1rem;
          flex-wrap: wrap;
        }
        .rep-header h1 {
          font-size: 1.6rem;
          font-weight: 700;
          letter-spacing: -0.02em;
          margin: 0 0 0.25rem 0;
        }
        .rep-header .subtitle {
          font-size: 0.75rem;
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.06em;
          text-transform: uppercase;
        }
        .rep-nav {
          display: flex;
          gap: 0.5rem;
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.7rem;
          letter-spacing: 0.06em;
          text-transform: uppercase;
        }
        .rep-nav a {
          color: var(--color-muted);
          text-decoration: none;
          border: 1px solid var(--color-border);
          padding: 0.35rem 0.7rem;
          transition: all 0.12s ease;
        }
        .rep-nav a:hover {
          color: var(--color-foreground);
          border-color: var(--color-accent);
        }

        .rep-filters {
          display: flex;
          flex-wrap: wrap;
          gap: 0.75rem;
          margin-bottom: 1.5rem;
          padding: 0.75rem;
          border: 1px solid var(--color-border);
          background: var(--color-surface);
        }
        .rep-filter {
          display: flex;
          flex-direction: column;
          gap: 0.25rem;
          min-width: 11rem;
        }
        .rep-filter label {
          font-size: 0.65rem;
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.08em;
          text-transform: uppercase;
          color: var(--color-muted);
        }
        .rep-filter select {
          padding: 0.35rem 0.45rem;
          font-size: 0.8rem;
          font-family: inherit;
          border: 1px solid var(--color-border);
          background: var(--color-background);
          color: inherit;
          border-radius: 0;
        }
        .rep-filter select:focus {
          outline: none;
          border-color: var(--color-accent);
        }
        .rep-clear {
          align-self: flex-end;
          font-size: 0.7rem;
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.04em;
          text-transform: uppercase;
          padding: 0.35rem 0.7rem;
          border: 1px solid var(--color-border);
          background: transparent;
          color: var(--color-muted);
          cursor: pointer;
        }
        .rep-clear:hover {
          color: var(--color-foreground);
          border-color: var(--color-accent);
        }

        .rep-total {
          font-size: 0.75rem;
          color: var(--color-muted);
          margin-bottom: 1rem;
          font-family: var(--font-geist-mono), monospace;
        }

        .rep-source-block {
          margin-bottom: 2rem;
          border: 1px solid var(--color-border);
        }
        .rep-source-header {
          display: flex;
          align-items: baseline;
          justify-content: space-between;
          padding: 0.55rem 0.75rem;
          border-bottom: 1px solid var(--color-border);
          background: var(--color-surface);
        }
        .rep-source-name {
          font-size: 0.85rem;
          font-weight: 600;
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.02em;
        }
        .rep-source-units {
          font-size: 0.7rem;
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
        }

        .rep-table {
          width: 100%;
          border-collapse: collapse;
          font-size: 0.82rem;
        }
        .rep-table th, .rep-table td {
          text-align: left;
          padding: 0.4rem 0.75rem;
          border-bottom: 1px solid var(--color-border);
        }
        .rep-table th {
          font-size: 0.65rem;
          text-transform: uppercase;
          letter-spacing: 0.08em;
          color: var(--color-muted);
          font-weight: 500;
          font-family: var(--font-geist-mono), monospace;
          background: var(--color-surface);
        }
        .rep-table td.num {
          font-family: var(--font-geist-mono), monospace;
          text-align: right;
          font-variant-numeric: tabular-nums;
        }
        .rep-table td.dim {
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.8rem;
        }
        .rep-table tbody tr:last-child td {
          border-bottom: none;
        }
        .rep-table tbody tr:hover {
          background: var(--color-surface);
        }

        .rep-range {
          font-size: 0.7rem;
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
          margin-left: 0.35rem;
        }

        .rep-mean {
          font-weight: 600;
        }
        .rep-mean.pos { color: #3d7f53; }
        .rep-mean.neg { color: #b54a3b; }
        .rep-mean.zero { color: var(--color-muted); }

        .rep-orch-pill {
          display: inline-block;
          font-size: 0.65rem;
          font-family: var(--font-geist-mono), monospace;
          padding: 0.1rem 0.4rem;
          border: 1px solid var(--color-border);
          border-radius: 0;
          color: var(--color-muted);
        }
        .rep-orch-pill.none {
          opacity: 0.4;
          border-style: dashed;
        }

        .rep-qc {
          margin-top: 2.5rem;
          border: 1px solid var(--color-border);
        }
        .rep-qc-header {
          display: flex;
          align-items: baseline;
          justify-content: space-between;
          padding: 0.55rem 0.75rem;
          border-bottom: 1px solid var(--color-border);
          background: var(--color-surface);
        }
        .rep-qc-title {
          font-size: 0.85rem;
          font-weight: 600;
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.02em;
        }
        .rep-qc-subtitle {
          font-size: 0.7rem;
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
        }
        .rep-qc-list {
          display: flex;
          flex-direction: column;
        }
        .rep-qc-row {
          display: grid;
          grid-template-columns: 5.5rem 9rem 1fr;
          gap: 0.75rem;
          padding: 0.55rem 0.75rem;
          border-bottom: 1px solid var(--color-border);
          align-items: baseline;
          font-size: 0.8rem;
        }
        .rep-qc-row:last-child {
          border-bottom: none;
        }
        .rep-qc-sev {
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.65rem;
          text-transform: uppercase;
          letter-spacing: 0.08em;
          padding: 0.1rem 0.4rem;
          border: 1px solid var(--color-border);
          align-self: start;
          justify-self: start;
        }
        .rep-qc-sev.critical {
          color: #b54a3b;
          border-color: #b54a3b;
        }
        .rep-qc-sev.moderate {
          color: #a87a2b;
          border-color: #a87a2b;
        }
        .rep-qc-sev.low {
          color: var(--color-muted);
        }
        .rep-qc-kind {
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.75rem;
          color: var(--color-muted);
        }
        .rep-qc-body {
          display: flex;
          flex-direction: column;
          gap: 0.3rem;
        }
        .rep-qc-evidence {
          line-height: 1.4;
        }
        .rep-qc-fix {
          font-size: 0.72rem;
          color: var(--color-muted);
          font-style: italic;
        }
        .rep-qc-pages {
          display: flex;
          flex-wrap: wrap;
          gap: 0.3rem;
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.7rem;
        }
        .rep-qc-pages a {
          color: var(--color-muted);
          text-decoration: none;
          padding: 0.05rem 0.35rem;
          border: 1px solid var(--color-border);
          transition: all 0.12s ease;
        }
        .rep-qc-pages a:hover {
          color: var(--color-foreground);
          border-color: var(--color-accent);
        }

        .rep-events {
          margin-top: 2.5rem;
        }
        .rep-events h2 {
          font-size: 0.75rem;
          font-weight: 600;
          letter-spacing: 0.06em;
          text-transform: uppercase;
          color: var(--color-muted);
          margin: 0 0 0.5rem 0;
          font-family: var(--font-geist-mono), monospace;
        }
        .rep-events-list {
          border: 1px solid var(--color-border);
          background: var(--color-border);
          display: flex;
          flex-direction: column;
          gap: 1px;
        }
        .rep-event-row {
          background: var(--color-background);
          display: grid;
          grid-template-columns: 10rem 9rem 9rem 4rem 1fr;
          gap: 0.5rem;
          padding: 0.4rem 0.75rem;
          font-size: 0.75rem;
          font-family: var(--font-geist-mono), monospace;
          align-items: baseline;
        }
        .rep-event-ts {
          color: var(--color-muted);
        }
        .rep-event-source {
          font-weight: 600;
        }
        .rep-event-dim {
          color: var(--color-muted);
        }
        .rep-event-score {
          text-align: right;
          font-variant-numeric: tabular-nums;
        }
        .rep-event-orch {
          color: var(--color-muted);
        }

        .rep-empty {
          padding: 2rem 1rem;
          text-align: center;
          color: var(--color-muted);
          font-size: 0.85rem;
          font-family: var(--font-geist-mono), monospace;
          letter-spacing: 0.04em;
          border: 1px dashed var(--color-border);
        }

        .rep-loading, .rep-error {
          padding: 4rem 1rem;
          text-align: center;
          color: var(--color-muted);
          font-family: var(--font-geist-mono), monospace;
          font-size: 0.8rem;
          letter-spacing: 0.04em;
        }
        .rep-loading { animation: pulse 1.5s ease infinite; }
        @keyframes pulse {
          0%, 100% { opacity: 0.5; }
          50% { opacity: 1; }
        }
      `}</style>

      <WorkspaceIndicator projectId={projectId} projectName={projectName} />

      <div className="rep-header">
        <div>
          <h1>Reputation</h1>
          <div className="subtitle">
            {projectName ? `${projectName} \u00b7 ` : ""}raw reputation signals
          </div>
        </div>
        <div className="rep-nav">
          <Link href={`/projects/${projectId}`}>Pages</Link>
          <Link href={`/projects/${projectId}/stats`}>Stats</Link>
        </div>
      </div>

      <div className="rep-filters">
        <div className="rep-filter">
          <label>Orchestrator</label>
          <select
            value={orchestrator}
            onChange={(e) => setOrchestrator(e.target.value)}
          >
            <option value="">all</option>
            {orchestratorOptions.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
        </div>
        <div className="rep-filter">
          <label>Source</label>
          <select value={source} onChange={(e) => setSource(e.target.value)}>
            <option value="">all</option>
            {sourceOptions.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <div className="rep-filter">
          <label>Dimension</label>
          <select
            value={dimension}
            onChange={(e) => setDimension(e.target.value)}
          >
            <option value="">all</option>
            {dimensionOptions.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </div>
        {hasFilters && (
          <button
            className="rep-clear"
            onClick={() => {
              setOrchestrator("");
              setSource("");
              setDimension("");
            }}
          >
            Clear
          </button>
        )}
      </div>

      {loading ? (
        <div className="rep-loading">Loading reputation{"\u2026"}</div>
      ) : error ? (
        <div className="rep-error">Failed to load: {error}</div>
      ) : !summary ? null : (
        <>
          <div className="rep-total">
            {summary.total_events} event{summary.total_events === 1 ? "" : "s"}
            {" \u00b7 "}
            {summary.buckets.length} bucket
            {summary.buckets.length === 1 ? "" : "s"}
          </div>

          {summary.buckets.length === 0 ? (
            <div className="rep-empty">
              No reputation events yet
              {hasFilters ? " for the current filters." : "."}
            </div>
          ) : (
            Object.entries(bySource).map(([sourceName, buckets]) => (
              <div key={sourceName} className="rep-source-block">
                <div className="rep-source-header">
                  <span className="rep-source-name">{sourceName}</span>
                  <span className="rep-source-units">
                    {sourceUnitsHint(sourceName)}
                  </span>
                </div>
                <table className="rep-table">
                  <thead>
                    <tr>
                      <th>Dimension</th>
                      <th>Orchestrator</th>
                      <th style={{ textAlign: "right" }}>N</th>
                      <th style={{ textAlign: "right" }}>Mean</th>
                      <th style={{ textAlign: "right" }}>Range</th>
                      <th>Latest</th>
                    </tr>
                  </thead>
                  <tbody>
                    {buckets.map((b) => {
                      const meanClass =
                        b.mean_score > 0
                          ? "pos"
                          : b.mean_score < 0
                            ? "neg"
                            : "zero";
                      return (
                        <tr
                          key={`${b.dimension}::${b.orchestrator ?? ""}`}
                        >
                          <td className="dim">{b.dimension}</td>
                          <td>
                            {b.orchestrator ? (
                              <span className="rep-orch-pill">
                                {b.orchestrator}
                              </span>
                            ) : (
                              <span className="rep-orch-pill none">
                                none
                              </span>
                            )}
                          </td>
                          <td className="num">{b.n_events}</td>
                          <td className="num">
                            <span
                              className={`rep-mean ${meanClass}`}
                              title={`min ${formatScore(b.min_score)} \u00b7 max ${formatScore(b.max_score)}`}
                            >
                              {formatScore(b.mean_score)}
                            </span>
                          </td>
                          <td className="num">
                            <span className="rep-range">
                              {formatScore(b.min_score)}
                              {" \u2026 "}
                              {formatScore(b.max_score)}
                            </span>
                          </td>
                          <td className="rep-event-ts">
                            {formatTs(b.latest_at)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ))
          )}

          {qcFindings.length > 0 && (
            <div className="rep-qc">
              <div className="rep-qc-header">
                <span className="rep-qc-title">Quality issues</span>
                <span className="rep-qc-subtitle">
                  {qcFindings.length} finding
                  {qcFindings.length === 1 ? "" : "s"} from quality_control
                  agent
                </span>
              </div>
              <div className="rep-qc-list">
                {qcFindings.map((f) => {
                  const extra = (f.extra ?? {}) as {
                    kind?: string;
                    severity?: string;
                    page_ids?: string[];
                    evidence?: string;
                    suggested_fix?: string;
                  };
                  const sev = (extra.severity ?? "low").toLowerCase();
                  const kind = extra.kind ?? "other";
                  const pages = extra.page_ids ?? [];
                  return (
                    <div key={f.id} className="rep-qc-row">
                      <span className={`rep-qc-sev ${sev}`}>{sev}</span>
                      <span className="rep-qc-kind">{kind}</span>
                      <div className="rep-qc-body">
                        <span className="rep-qc-evidence">
                          {extra.evidence ?? "(no evidence given)"}
                        </span>
                        {extra.suggested_fix && (
                          <span className="rep-qc-fix">
                            fix: {extra.suggested_fix}
                          </span>
                        )}
                        {pages.length > 0 && (
                          <div className="rep-qc-pages">
                            {pages.map((pid) => (
                              <Link
                                key={pid}
                                href={`/projects/${projectId}/pages/${pid}`}
                              >
                                {pid.length > 10
                                  ? `${pid.slice(0, 8)}\u2026`
                                  : pid}
                              </Link>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          <div className="rep-events">
            <h2>Recent events</h2>
            {events.length === 0 ? (
              <div className="rep-empty">No recent events.</div>
            ) : (
              <div className="rep-events-list">
                {events.map((e) => (
                  <div key={e.id} className="rep-event-row">
                    <span className="rep-event-ts">
                      {formatTs(e.created_at)}
                    </span>
                    <span className="rep-event-source">{e.source}</span>
                    <span className="rep-event-dim">{e.dimension}</span>
                    <span className="rep-event-score">
                      {formatScore(e.score)}
                    </span>
                    <span className="rep-event-orch">
                      {e.orchestrator ?? "\u2014"}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </main>
  );
}
