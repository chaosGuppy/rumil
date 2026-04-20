"use client";

import { useEffect, useState } from "react";
import { fetchBoundaryExchangeDetail } from "@/lib/api";
import type { BoundaryExchangeDetail } from "@/lib/api";

function MetaRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="op-boundary-meta-row">
      <span className="op-boundary-meta-label">{label}</span>
      <span className="op-boundary-meta-value">{value ?? "—"}</span>
    </div>
  );
}

function copy(text: string) {
  if (typeof navigator !== "undefined" && navigator.clipboard) {
    void navigator.clipboard.writeText(text);
  }
}

export function BoundaryDetailDrawer({
  exchangeId,
  onClose,
}: {
  exchangeId: string;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<BoundaryExchangeDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDetail(null);
    setError(null);
    fetchBoundaryExchangeDetail(exchangeId)
      .then(setDetail)
      .catch((e) => setError(e.message));
  }, [exchangeId]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="op-boundary-drawer-backdrop"
      onClick={onClose}
      role="dialog"
      aria-label="Exchange detail"
    >
      <aside
        className="op-boundary-drawer"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="op-boundary-drawer-header">
          <span className="op-boundary-drawer-id">{exchangeId.slice(0, 8)}</span>
          <button
            type="button"
            className="op-filter-btn"
            onClick={onClose}
            aria-label="Close"
          >
            close (esc)
          </button>
        </header>

        {error ? (
          <div className="op-trace-list-empty">Error: {error}</div>
        ) : !detail ? (
          <div className="op-trace-list-empty">Loading…</div>
        ) : (
          <div className="op-boundary-drawer-body">
            <section className="op-boundary-meta">
              <MetaRow label="source" value={detail.source} />
              <MetaRow label="streamed" value={detail.streamed ? "yes" : "no"} />
              <MetaRow label="model" value={detail.model} />
              <MetaRow label="started_at" value={detail.started_at} />
              <MetaRow label="finished_at" value={detail.finished_at ?? "—"} />
              <MetaRow
                label="latency_ms"
                value={detail.latency_ms ?? "—"}
              />
              <MetaRow label="stop_reason" value={detail.stop_reason ?? "—"} />
              <MetaRow
                label="usage"
                value={
                  detail.usage ? (
                    <code>{JSON.stringify(detail.usage)}</code>
                  ) : (
                    "—"
                  )
                }
              />
              <MetaRow label="run_id" value={detail.run_id ?? "—"} />
              <MetaRow label="call_id" value={detail.call_id ?? "—"} />
              {detail.error_class ? (
                <>
                  <MetaRow label="error_class" value={detail.error_class} />
                  <MetaRow
                    label="http_status"
                    value={detail.http_status ?? "—"}
                  />
                  <MetaRow
                    label="error_message"
                    value={detail.error_message ?? "—"}
                  />
                </>
              ) : null}
            </section>

            <section className="op-boundary-json-block">
              <header className="op-boundary-json-block-header">
                <h3>request_json</h3>
                <button
                  type="button"
                  className="op-filter-btn"
                  onClick={() =>
                    copy(JSON.stringify(detail.request_json, null, 2))
                  }
                >
                  copy
                </button>
              </header>
              <pre className="op-boundary-json">
                {JSON.stringify(detail.request_json, null, 2)}
              </pre>
            </section>

            <section className="op-boundary-json-block">
              <header className="op-boundary-json-block-header">
                <h3>response_json</h3>
                {detail.response_json ? (
                  <button
                    type="button"
                    className="op-filter-btn"
                    onClick={() =>
                      copy(JSON.stringify(detail.response_json, null, 2))
                    }
                  >
                    copy
                  </button>
                ) : null}
              </header>
              <pre className="op-boundary-json">
                {detail.response_json
                  ? JSON.stringify(detail.response_json, null, 2)
                  : "(none — request errored before response)"}
              </pre>
            </section>
          </div>
        )}
      </aside>
    </div>
  );
}
