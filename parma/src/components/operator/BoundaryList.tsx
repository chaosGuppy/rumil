"use client";

import { useEffect, useMemo, useState } from "react";
import { fetchBoundaryExchanges } from "@/lib/api";
import type { BoundaryExchange, PaginatedBoundaryExchanges } from "@/lib/api";
import { BoundaryDetailDrawer } from "./BoundaryDetailDrawer";

const SOURCE_PRESETS = [
  { label: "all", value: "" },
  { label: "chat", value: "chat" },
  { label: "llm.call_api", value: "llm.call_api" },
  { label: "structured_call", value: "llm.structured_call_parse" },
] as const;

const SINCE_PRESETS = [
  { label: "all time", value: "" },
  { label: "30m", seconds: 30 * 60 },
  { label: "2h", seconds: 2 * 60 * 60 },
  { label: "24h", seconds: 24 * 60 * 60 },
  { label: "7d", seconds: 7 * 24 * 60 * 60 },
] as const;

const PAGE_SIZE = 50;

function shortModel(model: string): string {
  return model.startsWith("claude-") ? model.slice("claude-".length) : model;
}

function fmtMs(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

function fmtTokens(usage: BoundaryExchange["usage"]): string {
  if (!usage) return "—";
  const inp = (usage.input_tokens as number | undefined) ?? 0;
  const out = (usage.output_tokens as number | undefined) ?? 0;
  const cacheR = (usage.cache_read_input_tokens as number | undefined) ?? 0;
  const cacheC = (usage.cache_creation_input_tokens as number | undefined) ?? 0;
  const base = `${inp}/${out}`;
  return cacheR || cacheC ? `${base}  c:${cacheR}/${cacheC}` : base;
}

function fmtTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function statusOf(row: BoundaryExchange): {
  text: string;
  cls: string;
} {
  if (row.error_class) {
    return {
      text: `ERR ${row.error_class}${row.http_status ? ` ${row.http_status}` : ""}`,
      cls: "op-boundary-status-error",
    };
  }
  return { text: row.stop_reason ?? "—", cls: "op-boundary-status-ok" };
}

export function BoundaryList({ projectId }: { projectId: string }) {
  const [data, setData] = useState<PaginatedBoundaryExchanges | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<string>("");
  const [model, setModel] = useState<string>("");
  const [errorOnly, setErrorOnly] = useState(false);
  const [sinceSeconds, setSinceSeconds] = useState<number | null>(null);
  const [offset, setOffset] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);

  // Reset pagination when filters or workspace change.
  useEffect(() => {
    setOffset(0);
  }, [projectId, source, model, errorOnly, sinceSeconds]);

  const sinceIso = useMemo(() => {
    if (sinceSeconds == null) return undefined;
    return new Date(Date.now() - sinceSeconds * 1000).toISOString();
  }, [sinceSeconds]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchBoundaryExchanges({
      projectId,
      limit: PAGE_SIZE,
      offset,
      source: source || undefined,
      model: model || undefined,
      errorOnly: errorOnly || undefined,
      since: sinceIso,
    })
      .then((res) => {
        if (cancelled) return;
        setData(res);
        setError(null);
        setLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e.message);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, source, model, errorOnly, sinceIso, offset]);

  const items = data?.items ?? [];
  const totalCount = data?.total_count ?? 0;
  const pageStart = items.length === 0 ? 0 : offset + 1;
  const pageEnd = offset + items.length;

  return (
    <>
      <div className="op-boundary-controls">
        <div className="op-boundary-filter-group">
          <span className="op-boundary-filter-label">source</span>
          <div className="op-trace-list-filters">
            {SOURCE_PRESETS.map((p) => (
              <button
                key={p.label}
                type="button"
                className={`op-filter-btn ${source === p.value ? "active" : ""}`}
                onClick={() => setSource(p.value)}
              >
                {p.label}
              </button>
            ))}
          </div>
        </div>

        <div className="op-boundary-filter-group">
          <span className="op-boundary-filter-label">since</span>
          <div className="op-trace-list-filters">
            {SINCE_PRESETS.map((p) => {
              const seconds = "seconds" in p ? p.seconds : null;
              return (
                <button
                  key={p.label}
                  type="button"
                  className={`op-filter-btn ${sinceSeconds === seconds ? "active" : ""}`}
                  onClick={() => setSinceSeconds(seconds)}
                >
                  {p.label}
                </button>
              );
            })}
          </div>
        </div>

        <label className="op-boundary-filter-text">
          model
          <input
            type="text"
            value={model}
            placeholder="haiku, opus, sonnet…"
            onChange={(e) => setModel(e.target.value)}
          />
        </label>

        <label className="op-boundary-filter-check">
          <input
            type="checkbox"
            checked={errorOnly}
            onChange={(e) => setErrorOnly(e.target.checked)}
          />
          errors only
        </label>

        <div className="op-boundary-pager">
          <span className="op-boundary-pager-count">
            {pageStart}–{pageEnd} of {totalCount}
          </span>
          <button
            type="button"
            className="op-filter-btn"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            ← prev
          </button>
          <button
            type="button"
            className="op-filter-btn"
            disabled={pageEnd >= totalCount}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            next →
          </button>
        </div>
      </div>

      {error ? (
        <div className="op-trace-list-empty">Error: {error}</div>
      ) : loading && items.length === 0 ? (
        <div className="op-trace-list-empty">Loading…</div>
      ) : items.length === 0 ? (
        <div className="op-trace-list-empty">No exchanges match these filters.</div>
      ) : (
        <table className="op-boundary-table">
          <thead>
            <tr>
              <th>time</th>
              <th>source</th>
              <th>model</th>
              <th className="op-boundary-num">latency</th>
              <th>tokens (in/out [c:r/c])</th>
              <th>status</th>
              <th>id</th>
            </tr>
          </thead>
          <tbody>
            {items.map((row) => {
              const s = statusOf(row);
              return (
                <tr
                  key={row.id}
                  className="op-boundary-row"
                  onClick={() => setOpenId(row.id)}
                >
                  <td className="op-boundary-time">{fmtTime(row.started_at)}</td>
                  <td>
                    {row.source}
                    {row.streamed ? (
                      <span className="op-boundary-stream-tag">stream</span>
                    ) : null}
                  </td>
                  <td>{shortModel(row.model)}</td>
                  <td className="op-boundary-num">{fmtMs(row.latency_ms)}</td>
                  <td className="op-boundary-tokens">{fmtTokens(row.usage)}</td>
                  <td className={s.cls}>{s.text}</td>
                  <td className="op-boundary-id">{row.id.slice(0, 8)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {openId ? (
        <BoundaryDetailDrawer
          exchangeId={openId}
          onClose={() => setOpenId(null)}
        />
      ) : null}
    </>
  );
}
