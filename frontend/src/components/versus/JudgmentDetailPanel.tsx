"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import type { JudgmentDetail } from "@/api/types.gen";

const API_BASE =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

function localTracePath(url: string | null | undefined): string | null {
  if (!url) return null;
  const m = url.match(/\/traces\/[^?#]+/);
  return m ? m[0] : null;
}

export function JudgmentDetailPanel({
  selectedKey,
  onClose,
}: {
  selectedKey: string | null;
  onClose: () => void;
}) {
  const [data, setData] = useState<JudgmentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!selectedKey) {
      setData(null);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetch(`${API_BASE}/api/versus/judgments/by-key?key=${encodeURIComponent(selectedKey)}`)
      .then(async (r) => {
        if (cancelled) return;
        if (!r.ok) {
          setError(`HTTP ${r.status}`);
          setData(null);
        } else {
          const json: JudgmentDetail = await r.json();
          setData(json);
        }
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedKey]);

  useEffect(() => {
    if (!selectedKey) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedKey, onClose]);

  if (!selectedKey) return null;

  const trace = data ? localTracePath(data.rumil_trace_url) : null;
  const rawJson = data?.raw_response
    ? JSON.stringify(data.raw_response, null, 2)
    : null;

  return (
    <>
      <div className="detail-panel-backdrop" onClick={onClose} />
      <aside className="detail-panel" role="dialog" aria-label="Judgment detail">
        <div className="detail-header">
          <h2>Judgment detail</h2>
          {data && (
            <span className="detail-verdict-pill">
              {data.verdict ? `verdict: ${data.verdict}` : "no verdict"}
            </span>
          )}
          <button
            className="detail-close"
            onClick={onClose}
            aria-label="Close"
            type="button"
          >
            ✕
          </button>
        </div>
        <div className="detail-body">
          {loading && <em className="versus-muted">loading…</em>}
          {error && (
            <div className="versus-card error-border">
              Failed to load: {error}
            </div>
          )}
          {data && <DetailContent data={data} trace={trace} rawJson={rawJson} />}
        </div>
      </aside>
    </>
  );
}

function DetailContent({
  data,
  trace,
  rawJson,
}: {
  data: JudgmentDetail;
  trace: string | null;
  rawJson: string | null;
}) {
  return (
    <>
      <section className="detail-section">
        <div className="detail-section-label">Meta</div>
        <dl className="detail-meta-grid">
          <dt>judge</dt>
          <dd>{data.judge_model}</dd>
          <dt>criterion</dt>
          <dd>{data.criterion}</dd>
          <dt>essay</dt>
          <dd>{data.essay_id}</dd>
          <dt>pair</dt>
          <dd>
            {data.source_a} vs {data.source_b}
          </dd>
          <dt>display</dt>
          <dd>
            A = {data.display_first}, B = {data.display_second}
          </dd>
          {data.winner_source && (
            <>
              <dt>winner</dt>
              <dd>{data.winner_source}</dd>
            </>
          )}
          {data.ts && (
            <>
              <dt>ts</dt>
              <dd>{data.ts}</dd>
            </>
          )}
          {data.duration_s != null && (
            <>
              <dt>duration</dt>
              <dd>{data.duration_s.toFixed(2)}s</dd>
            </>
          )}
          {data.rumil_cost_usd != null && (
            <>
              <dt>cost</dt>
              <dd>${data.rumil_cost_usd.toFixed(4)}</dd>
            </>
          )}
          {data.preference_label && (
            <>
              <dt>preference</dt>
              <dd>{data.preference_label}</dd>
            </>
          )}
          {data.contamination_note && (
            <>
              <dt>contamination</dt>
              <dd>{data.contamination_note}</dd>
            </>
          )}
        </dl>
      </section>

      {data.rumil_trace_url && (
        <section className="detail-section">
          <div className="detail-section-label">Rumil trace</div>
          {trace ? (
            <Link href={trace}>{trace}</Link>
          ) : (
            <a href={data.rumil_trace_url} target="_blank" rel="noreferrer">
              {data.rumil_trace_url}
            </a>
          )}
        </section>
      )}

      {data.prompt && (
        <details className="detail-section" open>
          <summary className="detail-section-label" style={{ cursor: "pointer" }}>
            Prompt
          </summary>
          <pre className="detail-pre" style={{ marginTop: 6 }}>
            {data.prompt}
          </pre>
        </details>
      )}

      {data.reasoning_text && (
        <details className="detail-section" open>
          <summary className="detail-section-label" style={{ cursor: "pointer" }}>
            Reasoning / response
          </summary>
          <div className="detail-prose" style={{ marginTop: 6 }}>
            {data.reasoning_text}
          </div>
        </details>
      )}

      {rawJson && (
        <details className="detail-section">
          <summary className="detail-section-label" style={{ cursor: "pointer" }}>
            Raw response (JSON)
          </summary>
          <pre className="detail-pre" style={{ marginTop: 6 }}>
            {rawJson}
          </pre>
        </details>
      )}

      {!data.prompt && !data.reasoning_text && !data.rumil_trace_url && (
        <p className="versus-muted">
          No prompt, reasoning, or trace recorded for this row. (Human verdict
          rows often have only a note in `reasoning_text`.)
        </p>
      )}
    </>
  );
}
