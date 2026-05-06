"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import type {
  ExchangePlaygroundContext,
  ForkOut,
  ForkOverrides,
  ForkSummary,
} from "@/api";
import { ResponseText } from "../../../_components/ResponseText";
import { MessagesEditor } from "./_parts/MessagesEditor";
import { ToolsEditor } from "./_parts/ToolsEditor";
import type { DraftOverrides, Msg, Tool } from "./_parts/types";
import { EMPTY_DRAFT } from "./_parts/types";

const CLIENT_API_BASE = process.env.NEXT_PUBLIC_API_URL || "";

const MODELS = [
  "claude-opus-4-7",
  "claude-opus-4-6",
  "claude-sonnet-4-6",
  "claude-haiku-4-5-20251001",
];

function modelHasAdaptiveThinking(model: string): boolean {
  return (
    model.startsWith("claude-opus-4-7") ||
    model.startsWith("claude-opus-4-6") ||
    model.startsWith("claude-sonnet-4-6")
  );
}

const RESPONSE_PREVIEW_CHARS = 500;

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

// Convert the base context's user_messages (Array<{[k:string]: unknown}>)
// into our typed Msg[]. The base shape is loose so it round-trips
// arbitrary content blocks; we just narrow `role` to a string.
function baseMsgs(ctx: ExchangePlaygroundContext): Msg[] {
  const raw = ctx.user_messages ?? [];
  return raw.map((m) => {
    const obj = m as { role?: unknown; content?: unknown };
    return {
      role: typeof obj.role === "string" ? obj.role : "user",
      content: obj.content ?? "",
    };
  });
}

function baseTools(ctx: ExchangePlaygroundContext): Tool[] {
  const raw = ctx.tools ?? [];
  return raw.map((t) => {
    const obj = t as Record<string, unknown>;
    return {
      name: typeof obj.name === "string" ? obj.name : "",
      description: typeof obj.description === "string" ? obj.description : "",
      input_schema: obj.input_schema ?? {},
    };
  });
}

function buildOverrides(
  draft: DraftOverrides,
  baseModel: string | undefined,
): ForkOverrides {
  const o: ForkOverrides = {};
  if (draft.systemPrompt !== null) o.system_prompt = draft.systemPrompt;
  if (draft.messages !== null) {
    o.user_messages = draft.messages as Array<{ [k: string]: unknown }>;
  }
  if (draft.tools !== null) {
    o.tools = draft.tools as Array<{ [k: string]: unknown }>;
  }
  if (draft.model !== null && draft.model !== baseModel) o.model = draft.model;
  if (draft.temperature !== null) o.temperature = draft.temperature;
  if (draft.maxTokens !== null) o.max_tokens = draft.maxTokens;
  if (draft.thinkingOff !== null) o.thinking_off = draft.thinkingOff;
  return o;
}

// Pretty list of edited fields for the editor header. Returned in the
// order they appear in the editor itself so the chip row stays stable
// as the user edits.
function editedFieldChips(d: DraftOverrides): string[] {
  const out: string[] = [];
  if (d.systemPrompt !== null) out.push("system_prompt");
  if (d.messages !== null) out.push("messages");
  if (d.tools !== null) out.push("tools");
  if (d.model !== null) out.push("model");
  if (d.temperature !== null) out.push("temperature");
  if (d.maxTokens !== null) out.push("max_tokens");
  if (d.thinkingOff !== null) out.push("thinking_off");
  return out;
}

export function PlaygroundEditor({ ctx }: { ctx: ExchangePlaygroundContext }) {
  const baseSystemPrompt = ctx.system_prompt ?? "";
  const baseMessages = useMemo(() => baseMsgs(ctx), [ctx]);
  const baseToolsList = useMemo(() => baseTools(ctx), [ctx]);
  const baseModel = ctx.model ?? "";
  const baseTemperature = ctx.temperature ?? null;
  const baseMaxTokens = ctx.max_tokens ?? null;
  const baseThinkingOff = !!ctx.thinking_off;
  const baseHasThinking = !!ctx.has_thinking;

  const [draft, setDraft] = useState<DraftOverrides>(EMPTY_DRAFT);
  const [nSamples, setNSamples] = useState<string>("1");
  const [firing, setFiring] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [forks, setForks] = useState<ForkSummary[]>(ctx.forks ?? []);
  const [responseExpanded, setResponseExpanded] = useState(false);

  const editedChips = editedFieldChips(draft);
  const anyDiff = editedChips.length > 0;

  const effSystemPrompt = draft.systemPrompt ?? baseSystemPrompt;
  const effMessages = draft.messages ?? baseMessages;
  const effTools = draft.tools ?? baseToolsList;
  const effModel = draft.model ?? baseModel;
  const effTempStr =
    draft.temperature !== null ? String(draft.temperature) : "";
  const effMaxTokStr =
    draft.maxTokens !== null ? String(draft.maxTokens) : "";
  const effThinkingOff =
    draft.thinkingOff !== null ? draft.thinkingOff : baseThinkingOff;

  const showThinking = baseHasThinking || modelHasAdaptiveThinking(effModel);

  function patch(p: Partial<DraftOverrides>) {
    setDraft((prev) => ({ ...prev, ...p }));
  }

  function resetAll() {
    setDraft(EMPTY_DRAFT);
  }

  async function onFire() {
    setError(null);
    setFiring(true);
    try {
      const overrides = buildOverrides(draft, baseModel);
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
    if (f.model && f.model !== baseModel) {
      patch({ model: f.model });
    }
    if (f.temperature != null && f.temperature !== baseTemperature) {
      patch({ temperature: f.temperature });
    }
  }

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
          {responseExpanded ||
          (ctx.response_text ?? "").length <= RESPONSE_PREVIEW_CHARS ? (
            <ResponseText text={ctx.response_text || "(empty response)"} />
          ) : (
            <pre className="atlas-pg-response-pre">
              {(ctx.response_text ?? "").slice(0, RESPONSE_PREVIEW_CHARS) +
                "…"}
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
              <span className="atlas-pg-edit-summary">
                {editedChips.map((c) => (
                  <span key={c} className="atlas-pg-edited">
                    {c}
                  </span>
                ))}
                <button
                  type="button"
                  className="atlas-pg-reset"
                  onClick={resetAll}
                  title="reset every field to base"
                >
                  reset all
                </button>
              </span>
            ) : (
              <span className="atlas-pg-no-diff">
                no changes — every field inherits base
              </span>
            )}
          </span>
        </div>

        <div className="atlas-pg-grid">
          <FieldShell
            title="system prompt"
            modified={draft.systemPrompt !== null}
            onReset={() => patch({ systemPrompt: null })}
            meta={`${effSystemPrompt.length} chars`}
          >
            <textarea
              className="atlas-pg-textarea"
              value={effSystemPrompt}
              onChange={(e) => patch({ systemPrompt: e.target.value })}
              spellCheck={false}
            />
          </FieldShell>

          <FieldShell
            title={`messages (${effMessages.length})`}
            modified={draft.messages !== null}
            onReset={() => patch({ messages: null })}
            meta={
              draft.messages !== null
                ? `was ${baseMessages.length}`
                : `${baseMessages.length} from base`
            }
          >
            <MessagesEditor
              value={effMessages}
              onChange={(next) => patch({ messages: next })}
            />
          </FieldShell>

          <FieldShell
            title={`tools (${effTools.length})`}
            modified={draft.tools !== null}
            onReset={() => patch({ tools: null })}
            meta={
              draft.tools !== null
                ? `was ${baseToolsList.length}`
                : baseToolsList.length === 0
                  ? "none on base"
                  : `${baseToolsList.length} from base`
            }
          >
            <ToolsEditor
              value={effTools}
              baseTools={baseToolsList}
              onChange={(next) => patch({ tools: next })}
            />
          </FieldShell>

          <div className="atlas-pg-config">
            <div className="atlas-pg-pane-head">
              <span>sampling config</span>
            </div>

            <ConfigField
              label="model"
              modified={draft.model !== null}
              onReset={() => patch({ model: null })}
              baseHint={baseModel || "—"}
            >
              <select
                value={effModel}
                onChange={(e) => patch({ model: e.target.value })}
              >
                {!MODELS.includes(effModel) && effModel && (
                  <option value={effModel}>{effModel}</option>
                )}
                {MODELS.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </ConfigField>

            <ConfigField
              label="temperature"
              modified={draft.temperature !== null}
              onReset={() => patch({ temperature: null })}
              baseHint={
                baseTemperature != null ? String(baseTemperature) : "inherit"
              }
            >
              <input
                type="number"
                step="0.1"
                min="0"
                max="2"
                value={effTempStr}
                placeholder={
                  baseTemperature != null ? String(baseTemperature) : "inherit"
                }
                onChange={(e) => {
                  const v = e.target.value;
                  patch({ temperature: v === "" ? null : Number(v) });
                }}
              />
            </ConfigField>

            <ConfigField
              label="max tokens"
              modified={draft.maxTokens !== null}
              onReset={() => patch({ maxTokens: null })}
              baseHint={
                baseMaxTokens != null ? String(baseMaxTokens) : "inherit"
              }
            >
              <input
                type="number"
                step="64"
                min="1"
                value={effMaxTokStr}
                placeholder={
                  baseMaxTokens != null ? String(baseMaxTokens) : "inherit"
                }
                onChange={(e) => {
                  const v = e.target.value;
                  patch({ maxTokens: v === "" ? null : Number(v) });
                }}
              />
            </ConfigField>

            {showThinking && (
              <ConfigField
                label="thinking off"
                modified={draft.thinkingOff !== null}
                onReset={() => patch({ thinkingOff: null })}
                baseHint={baseThinkingOff ? "off on base" : "on on base"}
                isRow
              >
                <input
                  type="checkbox"
                  checked={effThinkingOff}
                  onChange={(e) => patch({ thinkingOff: e.target.checked })}
                />
              </ConfigField>
            )}

            <div className="atlas-pg-fire-block">
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
              {!anyDiff && !firing && (
                <p className="atlas-pg-fire-hint">
                  no overrides — fork will resample with the same config.
                </p>
              )}
              {error && <div className="atlas-pg-error">{error}</div>}
            </div>
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
                new Set(forks.map((f) => f.overrides_hash)).size === 1
                  ? ""
                  : "s"
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

function FieldShell({
  title,
  modified,
  onReset,
  meta,
  children,
}: {
  title: string;
  modified: boolean;
  onReset: () => void;
  meta?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={`atlas-pg-field-shell${modified ? " is-modified" : ""}`}>
      <div className="atlas-pg-pane-head">
        <span>{title}</span>
        {modified && <span className="atlas-pg-edited">edited</span>}
        {meta && <span className="atlas-pg-meta">{meta}</span>}
        <span className="atlas-pg-msg-grow" />
        {modified && (
          <button
            type="button"
            className="atlas-pg-reset"
            onClick={onReset}
            title="reset to base"
          >
            reset
          </button>
        )}
      </div>
      <div className="atlas-pg-field-body">{children}</div>
    </div>
  );
}

function ConfigField({
  label,
  modified,
  onReset,
  baseHint,
  isRow,
  children,
}: {
  label: string;
  modified: boolean;
  onReset: () => void;
  baseHint?: string;
  isRow?: boolean;
  children: React.ReactNode;
}) {
  return (
    <label className={`atlas-pg-field${isRow ? " is-row" : ""}`}>
      <span className="atlas-pg-field-label-row">
        <span>{label}</span>
        {modified && <em>·edited</em>}
        {modified && (
          <button
            type="button"
            className="atlas-pg-reset is-inline"
            onClick={(e) => {
              e.preventDefault();
              onReset();
            }}
          >
            reset
          </button>
        )}
        {baseHint && !modified && (
          <span className="atlas-pg-base-hint">{baseHint}</span>
        )}
      </span>
      {children}
    </label>
  );
}

function diffSummary(
  fork: ForkSummary,
  base: ExchangePlaygroundContext,
): string[] {
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
  const preview =
    expanded || !long ? text : text.slice(0, RESPONSE_PREVIEW_CHARS) + "…";
  const diffs = useMemo(() => diffSummary(fork, base), [fork, base]);
  return (
    <article className={`atlas-pg-fork${fork.has_error ? " is-error" : ""}`}>
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
          <Link href={`/traces/${base.run_id ?? ""}`}>trace</Link> for detail
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
            <button type="button" className="atlas-pg-expand" onClick={onLoad}>
              load into editor
            </button>
          </div>
        </div>
      )}
    </article>
  );
}
