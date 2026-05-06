"use client";
import { useState } from "react";

const STRING_PREVIEW = 500;

export function ToolCallView({
  name,
  input,
  matched = false,
  tool_use_id,
}: {
  name: string;
  input: { [key: string]: unknown };
  matched?: boolean;
  tool_use_id?: string;
}) {
  const [showJson, setShowJson] = useState(false);
  const entries = Object.entries(input ?? {});

  return (
    <div className={`atlas-tcv${matched ? " is-matched" : ""}`}>
      <div className="atlas-tcv-head">
        <span className="atlas-tcv-tag">tool_use</span>
        <span className="atlas-tcv-name">{name}</span>
        {matched && <span className="atlas-tcv-matched">matched</span>}
        {tool_use_id && (
          <span className="atlas-tcv-id" title={tool_use_id}>
            {tool_use_id.length > 10 ? tool_use_id.slice(0, 10) : tool_use_id}
          </span>
        )}
        <span className="atlas-tcv-spacer" />
        <button
          type="button"
          className="atlas-tcv-toggle"
          onClick={() => setShowJson((v) => !v)}
          aria-pressed={showJson}
        >
          {showJson ? "pretty" : "json"}
        </button>
      </div>
      <div className="atlas-tcv-body">
        {showJson ? (
          <pre className="atlas-tcv-raw">{JSON.stringify(input, null, 2)}</pre>
        ) : entries.length === 0 ? (
          <div className="atlas-tcv-empty">(no arguments)</div>
        ) : (
          <dl className="atlas-tcv-rows">
            {entries.map(([k, v]) => (
              <Row key={k} k={k} v={v} />
            ))}
          </dl>
        )}
      </div>
    </div>
  );
}

function Row({ k, v }: { k: string; v: unknown }) {
  return (
    <div className="atlas-tcv-row">
      <dt className="atlas-tcv-key">{k}</dt>
      <dd className="atlas-tcv-val">
        <Value v={v} />
      </dd>
    </div>
  );
}

function Value({ v, depth = 0 }: { v: unknown; depth?: number }) {
  if (v === null) return <span className="atlas-tcv-chip is-null">null</span>;
  if (v === undefined) return <span className="atlas-tcv-chip is-null">—</span>;
  if (typeof v === "boolean") {
    return <span className="atlas-tcv-chip is-bool">{String(v)}</span>;
  }
  if (typeof v === "number") {
    return <span className="atlas-tcv-chip is-num">{v}</span>;
  }
  if (typeof v === "string") {
    return <StringValue s={v} />;
  }
  if (Array.isArray(v)) {
    return <ArrayValue arr={v} depth={depth} />;
  }
  if (typeof v === "object") {
    return <ObjectValue obj={v as { [key: string]: unknown }} depth={depth} />;
  }
  return <span className="atlas-tcv-chip">{String(v)}</span>;
}

function StringValue({ s }: { s: string }) {
  const [expanded, setExpanded] = useState(false);
  const long = s.length > STRING_PREVIEW;
  const shown = expanded || !long ? s : s.slice(0, STRING_PREVIEW) + "…";
  return (
    <div className="atlas-tcv-string">
      <pre className="atlas-tcv-string-text">{shown}</pre>
      {long && (
        <button
          type="button"
          className="atlas-tcv-mini"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? "collapse" : `expand (${s.length.toLocaleString()} chars)`}
        </button>
      )}
    </div>
  );
}

function ArrayValue({ arr, depth }: { arr: unknown[]; depth: number }) {
  if (arr.length === 0) {
    return <span className="atlas-tcv-chip is-null">[]</span>;
  }
  const allPrimitive = arr.every(
    (x) =>
      x === null ||
      typeof x === "string" ||
      typeof x === "number" ||
      typeof x === "boolean",
  );
  if (allPrimitive) {
    return (
      <div className="atlas-tcv-chips">
        {arr.map((x, i) => (
          <span key={i} className="atlas-tcv-chip">
            {x === null ? "null" : String(x)}
          </span>
        ))}
      </div>
    );
  }
  return (
    <div className="atlas-tcv-nested">
      {arr.map((x, i) => (
        <div key={i} className="atlas-tcv-nested-item">
          <span className="atlas-tcv-nested-idx">[{i}]</span>
          <div className="atlas-tcv-nested-body">
            <Value v={x} depth={depth + 1} />
          </div>
        </div>
      ))}
    </div>
  );
}

function ObjectValue({
  obj,
  depth,
}: {
  obj: { [key: string]: unknown };
  depth: number;
}) {
  const entries = Object.entries(obj);
  if (entries.length === 0) {
    return <span className="atlas-tcv-chip is-null">{"{}"}</span>;
  }
  return (
    <dl className="atlas-tcv-rows is-nested">
      {entries.map(([k, v]) => (
        <div key={k} className="atlas-tcv-row">
          <dt className="atlas-tcv-key">{k}</dt>
          <dd className="atlas-tcv-val">
            <Value v={v} depth={depth + 1} />
          </dd>
        </div>
      ))}
    </dl>
  );
}
