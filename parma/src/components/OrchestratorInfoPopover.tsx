"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { fetchOrchestratorInfo, type OrchestratorInfo } from "@/lib/api";

interface OrchestratorInfoPopoverProps {
  variant: string;
  projectId?: string;
  anchorEl: HTMLElement | null;
  onClose: () => void;
}

let mermaidInitialized = false;

function MermaidDiagram({ source }: { source: string }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        if (!mermaidInitialized) {
          mermaid.initialize({ startOnLoad: false, theme: "neutral" });
          mermaidInitialized = true;
        }
        if (cancelled || !ref.current) return;
        const id = `orch-mermaid-${Math.random().toString(36).slice(2, 10)}`;
        const { svg } = await mermaid.render(id, source);
        if (!cancelled && ref.current) ref.current.innerHTML = svg;
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [source]);

  if (error) {
    return (
      <pre className="orch-info-diagram-fallback" title={error}>
        {source}
      </pre>
    );
  }
  return <div ref={ref} className="orch-info-diagram" />;
}

function formatCount(n: number): string {
  return n.toLocaleString();
}

export default function OrchestratorInfoPopover({
  variant,
  projectId,
  anchorEl,
  onClose,
}: OrchestratorInfoPopoverProps) {
  const [info, setInfo] = useState<OrchestratorInfo | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const [expandedCalls, setExpandedCalls] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setInfo(null);
    setLoadError(null);
    fetchOrchestratorInfo(variant, projectId)
      .then((data) => {
        if (!cancelled) setInfo(data);
      })
      .catch((err: unknown) => {
        if (!cancelled)
          setLoadError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [variant, projectId]);

  useLayoutEffect(() => {
    if (!anchorEl) return;
    const rect = anchorEl.getBoundingClientRect();
    const width = 520;
    const left = Math.max(
      12,
      Math.min(rect.left, window.innerWidth - width - 12),
    );
    const top = rect.bottom + 6;
    setPos({ top, left });
  }, [anchorEl]);

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const handleClick = (e: MouseEvent) => {
      const target = e.target as Node;
      if (popoverRef.current && popoverRef.current.contains(target)) return;
      if (anchorEl && anchorEl.contains(target)) return;
      onClose();
    };
    document.addEventListener("keydown", handleKey);
    document.addEventListener("mousedown", handleClick);
    return () => {
      document.removeEventListener("keydown", handleKey);
      document.removeEventListener("mousedown", handleClick);
    };
  }, [onClose, anchorEl]);

  if (!pos) return null;

  const observedEntries: Array<[string, number]> = info
    ? Object.entries(info.observed_behavior.call_type_counts).map(
        ([k, v]) => [k, v as number],
      )
    : [];
  const totalObservedCalls = observedEntries.reduce(
    (sum, [, v]) => sum + v,
    0,
  );
  const sortedObserved = [...observedEntries].sort((a, b) => b[1] - a[1]);

  const body = (
    <div
      ref={popoverRef}
      className="orch-info-popover"
      style={{ top: pos.top, left: pos.left }}
      role="dialog"
      aria-label={`Orchestrator info: ${variant}`}
    >
      <div className="orch-info-header">
        <span className="orch-info-variant">{variant}</span>
        {info && (
          <>
            <span
              className={`orch-info-stability orch-info-stability-${info.stability}`}
              title={`stability: ${info.stability}`}
            >
              {info.stability}
            </span>
            <span
              className={`orch-info-cost orch-info-cost-${info.cost_band}`}
              title={`cost band: ${info.cost_band}`}
            >
              ${info.cost_band}
            </span>
          </>
        )}
        <button
          type="button"
          className="orch-info-close"
          onClick={onClose}
          aria-label="Close"
        >
          ×
        </button>
      </div>

      {loadError && <div className="orch-info-error">Failed to load: {loadError}</div>}

      {info && (
        <div className="orch-info-body">
          <p className="orch-info-description">{info.description}</p>

          {info.overview && (
            <section className="orch-info-section">
              <h4>Overview</h4>
              <p>{info.overview}</p>
            </section>
          )}

          {info.phases.length > 0 && (
            <section className="orch-info-section">
              <h4>
                Phases
                {info.phases.some((p) => p.source === "policy") && (
                  <span
                    className="orch-info-derived-hint"
                    title="derived from the live policy composition — cannot drift from code"
                  >
                    live
                  </span>
                )}
              </h4>
              <ol className="orch-info-phases">
                {info.phases.map((phase, i) => (
                  <li key={i}>
                    {phase.name && (
                      <span className="orch-info-phase-name">{phase.name}</span>
                    )}
                    <span className="orch-info-phase-desc">
                      {phase.description}
                    </span>
                  </li>
                ))}
              </ol>
            </section>
          )}

          {info.diagram_mermaid && (
            <section className="orch-info-section">
              <h4>Flow</h4>
              <MermaidDiagram source={info.diagram_mermaid} />
            </section>
          )}

          {info.related_call_types.length > 0 && (
            <section className="orch-info-section">
              <h4>
                Related call types
                <button
                  type="button"
                  className="orch-info-expand-toggle"
                  onClick={() => setExpandedCalls((v) => !v)}
                  aria-expanded={expandedCalls}
                >
                  {expandedCalls ? "collapse" : "expand"}
                </button>
              </h4>
              {expandedCalls ? (
                <ul className="orch-info-call-list">
                  {info.related_call_types.map((c) => (
                    <li key={c.value}>
                      <code>{c.value}</code>
                      <span className="orch-info-call-desc">
                        {c.description || "(no description)"}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="orch-info-call-chips">
                  {info.related_call_types.map((c) => (
                    <code
                      key={c.value}
                      className="orch-info-call-chip"
                      title={c.description}
                    >
                      {c.value}
                    </code>
                  ))}
                </div>
              )}
            </section>
          )}

          <section className="orch-info-section">
            <h4>
              Observed behavior
              <span
                className="orch-info-derived-hint"
                title="live aggregate across the last N runs of this variant — the drift detector"
              >
                live
              </span>
            </h4>
            {info.observed_behavior.run_count === 0 ? (
              <p className="orch-info-empty">
                No recent runs{projectId ? " in this project" : ""}.
              </p>
            ) : (
              <>
                <div className="orch-info-observed-meta">
                  {formatCount(totalObservedCalls)} calls across{" "}
                  {formatCount(info.observed_behavior.run_count)} recent run
                  {info.observed_behavior.run_count === 1 ? "" : "s"}
                  {projectId ? " in this project" : ""}
                </div>
                <ul className="orch-info-histogram">
                  {sortedObserved.map(([ct, count]) => {
                    const pct = totalObservedCalls
                      ? (count / totalObservedCalls) * 100
                      : 0;
                    return (
                      <li key={ct}>
                        <code className="orch-info-histogram-label">{ct}</code>
                        <span className="orch-info-histogram-bar">
                          <span
                            className="orch-info-histogram-fill"
                            style={{ width: `${pct}%` }}
                          />
                        </span>
                        <span className="orch-info-histogram-count">
                          {formatCount(count)}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </>
            )}
          </section>
        </div>
      )}

      {!info && !loadError && (
        <div className="orch-info-loading">Loading…</div>
      )}
    </div>
  );

  return createPortal(body, document.body);
}
