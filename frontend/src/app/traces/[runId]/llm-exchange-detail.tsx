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

  const preview = content.slice(0, 200);
  const isLong = content.length > 200;

  return (
    <div className="mb-2">
      <button
        onClick={() => setOpen(!open)}
        className="text-xs font-medium text-gray-600 hover:text-gray-800"
      >
        {open ? "▼" : "▶"} {label} ({content.length.toLocaleString()} chars)
      </button>
      {open && (
        <pre className="mt-1 text-xs bg-gray-50 border rounded p-2 overflow-x-auto whitespace-pre-wrap max-h-96 overflow-y-auto">
          {content}
        </pre>
      )}
      {!open && isLong && (
        <pre className="mt-1 text-xs bg-gray-50 border rounded p-2 text-gray-400 truncate">
          {preview}...
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
    <div className="border-b border-gray-100 py-1.5">
      <button
        onClick={loadDetail}
        className="flex items-center gap-2 text-xs hover:bg-gray-50 w-full text-left px-1 py-0.5 rounded"
      >
        <span className="text-gray-400">{open ? "▼" : "▶"}</span>
        <span className="font-medium text-gray-700">{summary.phase}</span>
        <span className="text-gray-500">round {summary.round}</span>
        {summary.input_tokens != null && (
          <span className="text-gray-400">
            {summary.input_tokens}/{summary.output_tokens} tokens
          </span>
        )}
        {summary.error && (
          <span className="text-red-600 font-medium">error</span>
        )}
        {loading && (
          <span className="text-gray-400 animate-pulse">loading...</span>
        )}
      </button>

      {open && detail && (
        <div className="ml-4 mt-1 space-y-1">
          <CollapsiblePre label="System prompt" content={detail.system_prompt} />
          <CollapsiblePre label="User message" content={detail.user_message} />
          <CollapsiblePre
            label="Response"
            content={detail.response_text}
          />
          {detail.tool_calls.length > 0 && (
            <div>
              <span className="text-xs font-medium text-gray-600">
                Tool calls ({detail.tool_calls.length}):
              </span>
              {detail.tool_calls.map((tc, i) => (
                <details key={i} className="ml-2 text-xs">
                  <summary className="cursor-pointer text-gray-600 hover:text-gray-800">
                    {tc.name as string}
                  </summary>
                  <pre className="bg-gray-50 border rounded p-1.5 mt-0.5 overflow-x-auto whitespace-pre-wrap text-xs">
                    {JSON.stringify(tc.input, null, 2)}
                  </pre>
                  {tc.result ? (
                    <pre className="bg-gray-50 border-l-2 border-green-300 rounded p-1.5 mt-0.5 overflow-x-auto whitespace-pre-wrap text-xs">
                      {String(tc.result)}
                    </pre>
                  ) : null}
                </details>
              ))}
            </div>
          )}
          {detail.error && (
            <div className="text-xs text-red-600 bg-red-50 rounded p-1.5">
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
      <div className="text-xs text-gray-400 animate-pulse">
        Loading exchanges...
      </div>
    );
  }

  if (exchanges.length === 0) {
    return (
      <div className="text-xs text-gray-400 italic">No LLM exchanges recorded</div>
    );
  }

  return (
    <div className="border rounded p-2 bg-white">
      <div className="text-xs font-medium text-gray-600 mb-1">
        LLM Exchanges ({exchanges.length})
      </div>
      {exchanges.map((ex) => (
        <ExchangeRow key={ex.id} summary={ex} />
      ))}
    </div>
  );
}
