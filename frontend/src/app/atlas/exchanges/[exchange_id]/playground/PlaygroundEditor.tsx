"use client";

import { useMemo, useState } from "react";
import type {
  ExchangePlaygroundContext,
  ForkOut,
  ForkOverrides,
  ForkSummary,
} from "@/api";
import { ResponseText } from "../../../_components/ResponseText";
import { CrossLink } from "../../../_components/CrossLink";

const CLIENT_API_BASE = process.env.NEXT_PUBLIC_API_URL || "";

const MODELS = [
  "claude-opus-4-7",
  "claude-opus-4-6",
  "claude-sonnet-4-6",
  "claude-haiku-4-5-20251001",
];

const RESPONSE_PREVIEW_CHARS = 500;

type Diff = {
  systemPrompt: boolean;
  userMessage: boolean;
  model: boolean;
  temperature: boolean;
  maxTokens: boolean;
  thinkingOff: boolean;
};

function trim(s: string | null | undefined): string {
  return (s ?? "").trim();
}

function fmtCost(v: number | null | undefined): string {
  if (v == null) return "—";
  if (v >= 1) return `$${v.toFixed(2)}`;
  if (v >= 0.01) return `$${v.toFixed(3)}`;
  if (v > 0) return `$${v.toFixed(4)}`;
  return "$0";
}

function fmtDuration(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60_000).toFixed(1)}m`;
}

function fmtRel(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 60_000) return `${Math.max(0, Math.floor(ms / 1000))}s ago`;
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`;
  return `${Math.floor(ms / 86_400_000)}d ago`;
}

export function PlaygroundEditor({ ctx }: { ctx: ExchangePlaygroundContext }) {
  const baseSystemPrompt = ctx.system_prompt ?? "";
  const baseUserMessage = ctx.user_message ?? "";
  const baseModel = ctx.model ?? "";
  const baseTemperature = ctx.temperature ?? null;
  const baseMaxTokens = ctx.max_tokens ?? null;
  const baseThinkingOff = !!ctx.thinking_off;
  const baseHasThinking = !!ctx.has_thinking;

  const [systemPrompt, setSystemPrompt] = useState(baseSystemPrompt);
  const [userMessage, setUserMessage] = useState(baseUserMessage);
  const [model, setModel] = useState(baseModel);
  const [temperature, setTemperature] = useState<string>(
    baseTemperature != null ? String(baseTemperature) : "",
  );
  const [maxTokens, setMaxTokens] = useState<string>(
    baseMaxTokens != null ? String(baseMaxTokens) : "",
  );
  const [thinkingOff, setThinkingOff] = useState<boolean>(baseThinkingOff);
  const [nSamples, setNSamples] = useState<string>("1");
  const [firing, setFiring] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [forks, setForks] = useState<ForkSummary[]>(ctx.forks ?? []);
  const [responseExpanded, setResponseExpanded] = useState(false);

  const diff: Diff = {
    systemPrompt: systemPrompt !== baseSystemPrompt,
    userMessage: userMessage !== baseUserMessage,
    model: model !== baseModel && trim(model) !== "",
    temperature:
      temperature !== "" &&
      (baseTemperature == null || Number(temperature) !== baseTemperature),
    maxTokens:
      maxTokens !== "" &&
      (baseMaxTokens == null || Number(maxTokens) !== baseMaxTokens),
    thinkingOff: thinkingOff !== baseThinkingOff,
  };
  const anyDiff = Object.values(diff).some(Boolean);

  function buildOverrides(): ForkOverrides {
    const overrides: ForkOverrides = {};
    if (diff.systemPrompt) overrides.system_prompt = systemPrompt;
    if (diff.userMessage) {
      overrides.user_messages = [
        { role: "user", content: userMessage },
      ];
    }
    if (diff.model) overrides.model = model;
    if (diff.temperature) overrides.temperature = Number(temperature);
    if (diff.maxTokens) overrides.max_tokens = Number(maxTokens);
    if (diff.thinkingOff) overrides.thinking_off = thinkingOff;
    return overrides;
  }

  async function onFire() {
    setError(null);
    setFiring(true);
    try {
      const overrides = buildOverrides();
      const body = {
        base_exchange_id: ctx.exchange_id,
        overrides,
        n_samples: Math.max(1, Number(nSamples) || 1),
      };
      const res = await fetch(`${CLIENT_API_BASE}/api/exchange-forks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const txt = await res.text();
        throw new Error(`fire failed (${res.status}): ${txt.slice(0, 240)}`);
      }
      const created = (await res.json()) as ForkOut[];
      const newSummaries: ForkSummary[] = created.map((f) => ({
        id: f.id,
        overrides_hash: f.overrides_hash,
        sample_index: f.sample_index,
        model: f.model,
        temperature: f.temperature,
        response_text: f.response_text,
        has_error: !!f.error,
        cost_usd: f.cost_usd,
        duration_ms: f.duration_ms,
        created_at: f.created_at,
        created_by: f.created_by,
      }));
      setForks((prev) => [...newSummaries, ...prev]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setFiring(false);
    }
  }

  function loadForkIntoEditor(f: ForkSummary) {
    if (f.model) setModel(f.model);
    if (f.temperature != null) setTemperature(String(f.temperature));
  }

  const showThinking = baseHasThinking;

  return (
    <div className="atlas-pg">
      <section className="atlas-section">
        <div className="atlas-section-head">
          <h2>captured response</h2>
          <span className="atlas-section-meta">
            base · model {ctx.model ?? "—"} · {(ctx.response_text ?? "").length}
            {" "}chars
          </span>
        </div>
        <div className="atlas-pg-response">
          {responseExpanded || (ctx.response_text ?? "").length <= RESPONSE_PREVIEW_CHARS ? (
            <ResponseText text={ctx.response_text || "(empty response)"} />
          ) : (
            <pre className="atlas-pg-response-pre">
              {(ctx.response_text ?? "").slice(0, RESPONSE_PREVIEW_CHARS) + "…"}
            </pre>
          )}
          {(ctx.response_text ?? "").length > RESPONSE_PREVIEW_CHARS && (
            <button
              type="button"
              className="atlas-pg-expand"
              onClick={() => setResponseExpanded((v) => !v)}
            >
              {responseExpanded ? "collapse" : "expand"}
            </button>
          )}
        </div>
      </section>

      <section className="atlas-section">
        <div className="atlas-section-head">
          <h2>editor</h2>
          <span className="atlas-section-meta">
            {anyDiff ? (
              <span style={{ color: "var(--a-warm)" }}>
                {Object.entries(diff)
                  .filter(([, v]) => v)
                  .map(([k]) => k)
                  .join(" · ")}{" "}
                edited
              </span>
            ) : (
              <span>no changes — inherits base</span>
            )}
          </span>
        </div>
        <div className="atlas-pg-editor">
          <div className="atlas-pg-pane">
            <div className="atlas-pg-pane-head">
              <span>system prompt</span>
              {diff.systemPrompt && (
                <span className="atlas-pg-edited">edited</span>
              )}
              {diff.systemPrompt && (
                <button
                  type="button"
                  className="atlas-pg-reset"
                  onClick={() => setSystemPrompt(baseSystemPrompt)}
                >
                  reset
                </button>
              )}
            </div>
            <textarea
              className="atlas-pg-textarea"
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              spellCheck={false}
            />
          </div>
          <div className="atlas-pg-pane">
            <div className="atlas-pg-pane-head">
              <span>user message</span>
              {diff.userMessage && (
                <span className="atlas-pg-edited">edited</span>
              )}
              {diff.userMessage && (
                <button
                  type="button"
                  className="atlas-pg-reset"
                  onClick={() => setUserMessage(baseUserMessage)}
                >
                  reset
                </button>
              )}
            </div>
            <textarea
              className="atlas-pg-textarea"
              value={userMessage}
              onChange={(e) => setUserMessage(e.target.value)}
              spellCheck={false}
            />
            {(ctx.user_messages?.length ?? 0) > 1 && (
              <p className="atlas-pg-hint">
                base has {ctx.user_messages?.length} messages — only the flat
                user_message is editable here. Full message-array editing
                coming soon.
              </p>
            )}
          </div>
          <div className="atlas-pg-controls">
            <div className="atlas-pg-pane-head">
              <span>config</span>
            </div>
            <label className="atlas-pg-field">
              <span>model {diff.model && <em>·edited</em>}</span>
              <select
                value={model}
                onChange={(e) => setModel(e.target.value)}
              >
                {!MODELS.includes(model) && model && (
                  <option value={model}>{model}</option>
                )}
                {MODELS.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </label>
            <label className="atlas-pg-field">
              <span>
                temperature {diff.temperature && <em>·edited</em>}
              </span>
              <input
                type="number"
                step="0.1"
                min="0"
                max="2"
                value={temperature}
                placeholder={baseTemperature != null ? String(baseTemperature) : "inherit"}
                onChange={(e) => setTemperature(e.target.value)}
              />
            </label>
            <label className="atlas-pg-field">
              <span>max tokens {diff.maxTokens && <em>·edited</em>}</span>
              <input
                type="number"
                step="64"
                min="1"
                value={maxTokens}
                placeholder={baseMaxTokens != null ? String(baseMaxTokens) : "inherit"}
                onChange={(e) => setMaxTokens(e.target.value)}
              />
            </label>
            {showThinking && (
              <label className="atlas-pg-field is-row">
                <input
                  type="checkbox"
                  checked={thinkingOff}
                  onChange={(e) => setThinkingOff(e.target.checked)}
                />
                <span>thinking off {diff.thinkingOff && <em>·edited</em>}</span>
              </label>
            )}
            <label className="atlas-pg-field">
              <span>n samples</span>
              <input
                type="number"
                min="1"
                max="8"
                value={nSamples}
                onChange={(e) => setNSamples(e.target.value)}
              />
            </label>
            <button
              type="button"
              className="atlas-pg-fire"
              onClick={onFire}
              disabled={firing}
            >
              {firing ? "firing…" : "fire fork →"}
            </button>
            {error && <div className="atlas-pg-error">{error}</div>}
          </div>
        </div>
      </section>

      <section className="atlas-section">
        <div className="atlas-section-head">
          <h2>forks</h2>
          <span className="atlas-section-meta">
            {forks.length} fork{forks.length === 1 ? "" : "s"}
            {forks.length > 0 &&
              ` · ${new Set(forks.map((f) => f.overrides_hash)).size} distinct config${
                new Set(forks.map((f) => f.overrides_hash)).size === 1 ? "" : "s"
              }`}
          </span>
        </div>
        {forks.length === 0 ? (
          <div className="atlas-empty">
            <strong>no forks yet</strong>
            edit the prompt or config above and fire to see how the model
            would respond.
          </div>
        ) : (
          <div className="atlas-pg-forks">
            {forks.map((f) => (
              <ForkCard
                key={f.id}
                fork={f}
                base={ctx}
                onLoad={() => loadForkIntoEditor(f)}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function diffSummary(fork: ForkSummary, base: ExchangePlaygroundContext): string[] {
  const out: string[] = [];
  if (fork.model && fork.model !== base.model) {
    out.push(`model → ${fork.model}`);
  }
  if (
    fork.temperature != null &&
    fork.temperature !== (base.temperature ?? null)
  ) {
    const baseT = base.temperature ?? 0;
    const arrow = fork.temperature > baseT ? "↑" : "↓";
    out.push(`temp ${arrow} ${fork.temperature}`);
  }
  return out;
}

function ForkCard({
  fork,
  base,
  onLoad,
}: {
  fork: ForkSummary;
  base: ExchangePlaygroundContext;
  onLoad: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const text = fork.response_text ?? "";
  const long = text.length > RESPONSE_PREVIEW_CHARS;
  const preview = expanded || !long ? text : text.slice(0, RESPONSE_PREVIEW_CHARS) + "…";
  const diffs = useMemo(() => diffSummary(fork, base), [fork, base]);
  return (
    <article
      className={`atlas-pg-fork${fork.has_error ? " is-error" : ""}`}
    >
      <header className="atlas-pg-fork-head">
        <span className="atlas-pg-fork-hash" title={fork.overrides_hash}>
          {fork.overrides_hash.slice(0, 8)}
        </span>
        <span className="atlas-pg-fork-sample">#{fork.sample_index}</span>
        {diffs.length === 0 ? (
          <span className="atlas-pg-fork-diff is-base">same as base</span>
        ) : (
          diffs.map((d, i) => (
            <span key={i} className="atlas-pg-fork-diff">
              {d}
            </span>
          ))
        )}
        <span className="atlas-pg-fork-stats">
          {fmtCost(fork.cost_usd)} · {fmtDuration(fork.duration_ms)}
          {fork.created_at && <> · {fmtRel(fork.created_at)}</>}
        </span>
      </header>
      {fork.has_error ? (
        <div className="atlas-pg-fork-error">
          fork errored — see{" "}
          <CrossLink to={`/traces/${base.run_id ?? ""}`}>trace</CrossLink>{" "}
          for detail
        </div>
      ) : (
        <div className="atlas-pg-fork-body">
          {expanded || !long ? (
            <ResponseText text={preview || "(empty)"} />
          ) : (
            <pre className="atlas-pg-fork-pre">{preview || "(empty)"}</pre>
          )}
          <div className="atlas-pg-fork-actions">
            {long && (
              <button
                type="button"
                className="atlas-pg-expand"
                onClick={() => setExpanded((v) => !v)}
              >
                {expanded ? "collapse" : "expand"}
              </button>
            )}
            <button
              type="button"
              className="atlas-pg-expand"
              onClick={onLoad}
            >
              load into editor
            </button>
          </div>
        </div>
      )}
    </article>
  );
}
