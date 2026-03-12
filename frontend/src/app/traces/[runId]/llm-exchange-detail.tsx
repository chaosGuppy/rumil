"use client";

import { useEffect, useState } from "react";

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";

interface ExchangeSummary {
  id: string;
  phase: string;
  round: number;
  input_tokens: number | null;
  output_tokens: number | null;
  duration_ms: number | null;
  error: string | null;
  created_at: string;
}

interface ExchangeFull {
  id: string;
  call_id: string;
  phase: string;
  round: number;
  system_prompt: string | null;
  user_message: string | null;
  response_text: string | null;
  tool_calls: Array<Record<string, unknown>>;
  input_tokens: number | null;
  output_tokens: number | null;
  duration_ms: number | null;
  error: string | null;
  created_at: string;
}

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

function ExchangeRow({ summary }: { summary: ExchangeSummary }) {
  const [detail, setDetail] = useState<ExchangeFull | null>(null);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);

  async function loadDetail() {
    if (detail) {
      setOpen(!open);
      return;
    }
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/llm-exchanges/${summary.id}`);
      if (res.ok) {
        setDetail(await res.json());
        setOpen(true);
      }
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="trace-exchange-row">
      <button onClick={loadDetail} className="trace-exchange-toggle">
        <span className="trace-exchange-icon">{open ? "\u2013" : "+"}</span>
        <span className="trace-exchange-phase">{summary.phase}</span>
        <span className="trace-exchange-round">r{summary.round}</span>
        {summary.input_tokens != null && (
          <span className="trace-exchange-tokens">
            {summary.input_tokens.toLocaleString()}/{summary.output_tokens?.toLocaleString()} tok
          </span>
        )}
        {summary.duration_ms != null && (
          <span className="trace-exchange-duration">
            {(summary.duration_ms / 1000).toFixed(1)}s
          </span>
        )}
        {summary.error && (
          <span className="trace-exchange-error-flag">error</span>
        )}
        {loading && (
          <span className="trace-exchange-loading">loading...</span>
        )}
      </button>

      {open && detail && (
        <div className="trace-exchange-detail">
          <CollapsiblePre label="System prompt" content={detail.system_prompt} />
          <CollapsiblePre label="User message" content={detail.user_message} />
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
      )}
    </div>
  );
}

export function LLMExchangeDetail({ callId }: { callId: string }) {
  const [exchanges, setExchanges] = useState<ExchangeSummary[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`${API_BASE}/api/calls/${callId}/llm-exchanges`)
      .then((r) => (r.ok ? r.json() : []))
      .then(setExchanges)
      .finally(() => setLoading(false));
  }, [callId]);

  if (loading) {
    return (
      <div className="trace-exchange-loading">Loading exchanges...</div>
    );
  }

  if (exchanges.length === 0) {
    return (
      <div className="trace-empty">No LLM exchanges recorded</div>
    );
  }

  return (
    <div className="trace-exchanges-container">
      <div className="trace-section-label">
        LLM exchanges ({exchanges.length})
      </div>
      {exchanges.map((ex) => (
        <ExchangeRow key={ex.id} summary={ex} />
      ))}
    </div>
  );
}
