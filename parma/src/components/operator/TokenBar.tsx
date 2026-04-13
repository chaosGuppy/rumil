import type { TokenUsage } from "@/lib/operator-types";

function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  if (n >= 1_000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

export function TokenBar({
  usage,
  showLabels = false,
}: {
  usage: TokenUsage;
  showLabels?: boolean;
}) {
  const total =
    usage.input_tokens +
    usage.cache_read_tokens +
    usage.cache_write_tokens +
    usage.output_tokens;

  if (total === 0) return null;

  const segments = [
    { key: "input", value: usage.input_tokens, className: "op-token-input" },
    { key: "cache-read", value: usage.cache_read_tokens, className: "op-token-cache-read" },
    { key: "cache-write", value: usage.cache_write_tokens, className: "op-token-cache-write" },
    { key: "output", value: usage.output_tokens, className: "op-token-output" },
  ].filter((s) => s.value > 0);

  const cacheHitPct =
    usage.cache_read_tokens > 0
      ? Math.round(
          (usage.cache_read_tokens /
            (usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens)) *
            100,
        )
      : 0;

  return (
    <div className="op-token-bar-wrap">
      <div className="op-token-bar">
        {segments.map((s) => (
          <div
            key={s.key}
            className={`op-token-segment ${s.className}`}
            style={{ flex: s.value }}
            title={`${s.key}: ${s.value.toLocaleString()}`}
          />
        ))}
      </div>
      <div className="op-token-summary">
        <span className="op-token-label">
          {compact(usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens)} in
        </span>
        <span className="op-token-arrow">&rarr;</span>
        <span className="op-token-label">{compact(usage.output_tokens)} out</span>
        {cacheHitPct > 0 && (
          <span className="op-token-cache-pct">{cacheHitPct}% cached</span>
        )}
      </div>
      {showLabels && (
        <div className="op-token-breakdown">
          {usage.input_tokens > 0 && (
            <span className="op-token-detail">
              <span className="op-token-dot op-token-input" />
              input {compact(usage.input_tokens)}
            </span>
          )}
          {usage.cache_read_tokens > 0 && (
            <span className="op-token-detail">
              <span className="op-token-dot op-token-cache-read" />
              cache read {compact(usage.cache_read_tokens)}
            </span>
          )}
          {usage.cache_write_tokens > 0 && (
            <span className="op-token-detail">
              <span className="op-token-dot op-token-cache-write" />
              cache write {compact(usage.cache_write_tokens)}
            </span>
          )}
          {usage.output_tokens > 0 && (
            <span className="op-token-detail">
              <span className="op-token-dot op-token-output" />
              output {compact(usage.output_tokens)}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
