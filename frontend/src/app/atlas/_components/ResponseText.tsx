"use client";
import { useMemo, useState } from "react";

type Segment =
  | { kind: "text"; text: string }
  | { kind: "json"; raw: string; parsed: unknown };

const FENCE_RE = /```json\s*\n?([\s\S]*?)```/gi;

function tryParse(s: string): unknown | undefined {
  const trimmed = s.trim();
  if (!trimmed) return undefined;
  const first = trimmed[0];
  if (first !== "{" && first !== "[") return undefined;
  try {
    return JSON.parse(trimmed);
  } catch {
    return undefined;
  }
}

function segmentText(text: string): Segment[] {
  const segments: Segment[] = [];
  let last = 0;
  FENCE_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = FENCE_RE.exec(text)) !== null) {
    if (m.index > last) {
      segments.push({ kind: "text", text: text.slice(last, m.index) });
    }
    const inner = m[1] ?? "";
    const parsed = tryParse(inner);
    if (parsed !== undefined) {
      segments.push({ kind: "json", raw: inner, parsed });
    } else {
      segments.push({ kind: "text", text: m[0] });
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) {
    segments.push({ kind: "text", text: text.slice(last) });
  }
  return segments;
}

export function ResponseText({ text }: { text: string }) {
  const segments = useMemo<Segment[]>(() => {
    if (!text) return [{ kind: "text", text: "" }];
    const whole = tryParse(text);
    if (whole !== undefined) return [{ kind: "json", raw: text, parsed: whole }];
    return segmentText(text);
  }, [text]);

  if (segments.length === 0 || (segments.length === 1 && segments[0].kind === "text" && !segments[0].text)) {
    return <pre className="atlas-inv-text">(empty)</pre>;
  }

  return (
    <div className="atlas-rt">
      {segments.map((seg, i) =>
        seg.kind === "text" ? (
          <pre key={i} className="atlas-rt-text">
            {seg.text}
          </pre>
        ) : (
          <JsonBlock key={i} raw={seg.raw} parsed={seg.parsed} />
        ),
      )}
    </div>
  );
}

function JsonBlock({ raw, parsed }: { raw: string; parsed: unknown }) {
  const [showRaw, setShowRaw] = useState(false);
  return (
    <div className="atlas-rt-json">
      <div className="atlas-rt-json-head">
        <span className="atlas-rt-json-tag">json</span>
        <span className="atlas-rt-json-meta">
          {Array.isArray(parsed)
            ? `array · ${parsed.length} item${parsed.length === 1 ? "" : "s"}`
            : typeof parsed === "object" && parsed
              ? `object · ${Object.keys(parsed).length} key${Object.keys(parsed).length === 1 ? "" : "s"}`
              : typeof parsed}
        </span>
        <span className="atlas-rt-json-spacer" />
        <button
          type="button"
          className="atlas-tcv-toggle"
          onClick={() => setShowRaw((v) => !v)}
          aria-pressed={showRaw}
        >
          {showRaw ? "pretty" : "raw"}
        </button>
      </div>
      <div className="atlas-rt-json-body">
        {showRaw ? (
          <pre className="atlas-tcv-raw">{raw.trim()}</pre>
        ) : (
          <PrettyJson value={parsed} />
        )}
      </div>
    </div>
  );
}

const STRING_PREVIEW = 500;

function PrettyJson({ value }: { value: unknown }) {
  if (value === null) return <span className="atlas-tcv-chip is-null">null</span>;
  if (typeof value === "boolean") return <span className="atlas-tcv-chip is-bool">{String(value)}</span>;
  if (typeof value === "number") return <span className="atlas-tcv-chip is-num">{value}</span>;
  if (typeof value === "string") return <PrettyString s={value} />;
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="atlas-tcv-chip is-null">[]</span>;
    const allPrimitive = value.every(
      (x) =>
        x === null ||
        typeof x === "string" ||
        typeof x === "number" ||
        typeof x === "boolean",
    );
    if (allPrimitive) {
      return (
        <div className="atlas-tcv-chips">
          {value.map((x, i) => (
            <span key={i} className="atlas-tcv-chip">
              {x === null ? "null" : String(x)}
            </span>
          ))}
        </div>
      );
    }
    return (
      <div className="atlas-tcv-nested">
        {value.map((x, i) => (
          <div key={i} className="atlas-tcv-nested-item">
            <span className="atlas-tcv-nested-idx">[{i}]</span>
            <div className="atlas-tcv-nested-body">
              <PrettyJson value={x} />
            </div>
          </div>
        ))}
      </div>
    );
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as { [key: string]: unknown });
    if (entries.length === 0) return <span className="atlas-tcv-chip is-null">{"{}"}</span>;
    return (
      <dl className="atlas-tcv-rows is-nested">
        {entries.map(([k, v]) => (
          <div key={k} className="atlas-tcv-row">
            <dt className="atlas-tcv-key">{k}</dt>
            <dd className="atlas-tcv-val">
              <PrettyJson value={v} />
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  return <span className="atlas-tcv-chip">{String(value)}</span>;
}

function PrettyString({ s }: { s: string }) {
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
