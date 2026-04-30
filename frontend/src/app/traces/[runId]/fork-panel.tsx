"use client";

/**
 * Exchange Forks panel — admin-only inline UI for re-firing a captured
 * LLM exchange with edited overrides. Side-effect-free: any tool_use
 * blocks in responses are stored as data, never executed.
 *
 * Layout: column-per-config with samples stacked vertically inside each
 * column. Original column is read-only and reflects the captured response.
 * Variants are loaded from /api/exchange-forks and grouped by overrides_hash.
 * "+ New variant" creates an in-memory column whose form starts empty
 * (inherits everything from base) and only persists rows once the admin
 * clicks Sample.
 */

import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  BaseExchangeOut,
  ForkOut,
  ForkOverrides,
  LlmExchangeOut,
} from "@/api/types.gen";
import {
  fireApiExchangeForksPost,
  getBaseApiExchangeForksBaseExchangeIdGet,
  listForksApiExchangeForksGet,
  deleteForkApiExchangeForksForkIdDelete,
} from "@/api/sdk.gen";
import { CLIENT_API_BASE } from "@/api-config";
import { useCurrentUser } from "@/lib/use-current-user";

const MODELS = [
  "claude-opus-4-7",
  "claude-opus-4-6",
  "claude-sonnet-4-6",
  "claude-haiku-4-5-20251001",
];

type PricingRow = { input: number; output: number };
const MODEL_PRICING: Record<string, PricingRow> = {
  "claude-opus-4-7": { input: 5.0, output: 25.0 },
  "claude-opus-4-6": { input: 5.0, output: 25.0 },
  "claude-sonnet-4-6": { input: 3.0, output: 15.0 },
  "claude-haiku-4-5-20251001": { input: 1.0, output: 5.0 },
};

function modelSupportsSampling(model: string): boolean {
  if (model.startsWith("claude-opus-4-7")) return false;
  if (model.startsWith("claude-opus-4-6")) return false;
  if (model.startsWith("claude-sonnet-4-6")) return false;
  return true;
}

type Tool = { name: string; description: string; input_schema: unknown };
type Msg = { role: string; content: unknown };

type DraftOverrides = {
  systemPrompt: string | null;
  messages: Msg[] | null;
  tools: Tool[] | null;
  model: string | null;
  temperature: number | null;
  maxTokens: number | null;
};

const EMPTY_DRAFT: DraftOverrides = {
  systemPrompt: null,
  messages: null,
  tools: null,
  model: null,
  temperature: null,
  maxTokens: null,
};

function draftToOverrides(d: DraftOverrides): ForkOverrides {
  const o: ForkOverrides = {};
  if (d.systemPrompt !== null) o.system_prompt = d.systemPrompt;
  if (d.messages !== null) o.user_messages = d.messages as Array<{ [k: string]: unknown }>;
  if (d.tools !== null) o.tools = d.tools as Array<{ [k: string]: unknown }>;
  if (d.model !== null) o.model = d.model;
  if (d.temperature !== null) o.temperature = d.temperature;
  if (d.maxTokens !== null) o.max_tokens = d.maxTokens;
  return o;
}

function overridesToDraft(o: ForkOverrides): DraftOverrides {
  return {
    systemPrompt: o.system_prompt ?? null,
    messages: (o.user_messages as Msg[] | undefined) ?? null,
    tools: (o.tools as Tool[] | undefined) ?? null,
    model: o.model ?? null,
    temperature: o.temperature ?? null,
    maxTokens: o.max_tokens ?? null,
  };
}

function diffChips(d: DraftOverrides, base: BaseExchangeOut | null): string[] {
  if (!base) return [];
  const chips: string[] = [];
  if (d.systemPrompt !== null && d.systemPrompt !== base.system_prompt) {
    chips.push("system prompt edited");
  }
  if (d.messages !== null) {
    const baseMsgs = base.user_messages as Msg[];
    if (d.messages.length !== baseMsgs.length) {
      chips.push(`${d.messages.length} msg (was ${baseMsgs.length})`);
    } else {
      let changed = false;
      for (let i = 0; i < d.messages.length; i++) {
        const a = d.messages[i];
        const b = baseMsgs[i];
        if (a.role !== b.role || JSON.stringify(a.content) !== JSON.stringify(b.content)) {
          changed = true;
          break;
        }
      }
      if (changed) chips.push("messages edited");
    }
  }
  if (d.tools !== null) {
    const baseNames = new Set((base.tools as Tool[]).map((t) => t.name));
    const draftNames = new Set(d.tools.map((t) => t.name));
    const removed = [...baseNames].filter((n) => !draftNames.has(n));
    const added = [...draftNames].filter((n) => !baseNames.has(n));
    for (const n of removed) chips.push(`-${n}`);
    for (const n of added) chips.push(`+${n}`);
    if (removed.length === 0 && added.length === 0) {
      const baseTools = base.tools as Tool[];
      const edited: string[] = [];
      for (const t of d.tools) {
        const orig = baseTools.find((b) => b.name === t.name);
        if (
          orig &&
          (orig.description !== t.description ||
            JSON.stringify(orig.input_schema) !== JSON.stringify(t.input_schema))
        ) {
          edited.push(t.name);
        }
      }
      for (const n of edited) chips.push(`~${n}`);
    }
  }
  if (d.model !== null && d.model !== base.model) chips.push(`model: ${d.model}`);
  if (d.temperature !== null && d.temperature !== base.temperature) {
    chips.push(`temp ${d.temperature}`);
  }
  if (d.maxTokens !== null && d.maxTokens !== base.max_tokens) {
    chips.push(`max_tok ${d.maxTokens}`);
  }
  return chips;
}

function estimateCost(
  draftModel: string,
  inputTokens: number,
  expectedOutput: number,
  samples: number,
): number {
  const rates = MODEL_PRICING[draftModel];
  if (!rates) return 0;
  const input = (inputTokens * rates.input) / 1_000_000;
  const output = (expectedOutput * rates.output) / 1_000_000;
  return (input + output) * samples;
}

export function ForkTrigger({
  exchangeId,
  loadedDetail,
  onOpen,
  open,
}: {
  exchangeId: string;
  loadedDetail: LlmExchangeOut | null;
  open: boolean;
  onOpen: () => void;
}) {
  const user = useCurrentUser();
  const isAdmin = user?.is_admin ?? false;
  const enabled = isAdmin && exchangeId.length > 0 && Boolean(loadedDetail);
  const { data: forks } = useQuery({
    queryKey: ["forks", exchangeId, "summary"],
    queryFn: async () => {
      const res = await listForksApiExchangeForksGet({
        baseUrl: CLIENT_API_BASE,
        query: { base_exchange_id: exchangeId },
      });
      return (res.data ?? []) as ForkOut[];
    },
    enabled,
    staleTime: 30_000,
  });

  if (!isAdmin || !loadedDetail) return null;

  const count = forks?.length ?? 0;
  return (
    <button
      type="button"
      className="fork-trigger"
      onClick={onOpen}
      title={open ? "close forks panel" : "open forks panel — edit and re-fire this exchange"}
    >
      <span className="fork-trigger-icon">⑂</span>
      {open ? "close forks" : "fork & rerun"}
      {count > 0 && <span className="fork-trigger-count">{count}</span>}
    </button>
  );
}

export function ForkPanel({
  exchangeId,
  exchangeDetail,
  onClose,
}: {
  exchangeId: string;
  exchangeDetail: LlmExchangeOut;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();

  const { data: base } = useQuery({
    queryKey: ["forks", exchangeId, "base"],
    queryFn: async () => {
      const res = await getBaseApiExchangeForksBaseExchangeIdGet({
        baseUrl: CLIENT_API_BASE,
        path: { exchange_id: exchangeId },
      });
      return res.data as BaseExchangeOut;
    },
  });

  const { data: forks = [] } = useQuery({
    queryKey: ["forks", exchangeId, "list"],
    queryFn: async () => {
      const res = await listForksApiExchangeForksGet({
        baseUrl: CLIENT_API_BASE,
        query: { base_exchange_id: exchangeId },
      });
      return (res.data ?? []) as ForkOut[];
    },
  });

  // Group existing forks by overrides_hash, preserving creation order of
  // first-seen hash to keep column order stable as samples arrive.
  const variants = useMemo(() => {
    const byHash = new Map<string, ForkOut[]>();
    const order: string[] = [];
    for (const f of forks) {
      if (!byHash.has(f.overrides_hash)) {
        byHash.set(f.overrides_hash, []);
        order.push(f.overrides_hash);
      }
      byHash.get(f.overrides_hash)!.push(f);
    }
    return order.map((h) => ({
      hash: h,
      samples: byHash.get(h)!.sort((a, b) => a.sample_index - b.sample_index),
      overrides: byHash.get(h)![0]?.overrides ?? {},
    }));
  }, [forks]);

  const [openDrafts, setOpenDrafts] = useState<Record<string, DraftOverrides>>({});
  const [pendingByDraft, setPendingByDraft] = useState<Record<string, number>>({});

  function newVariantKey(): string {
    return `new-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
  }

  function addNewVariant() {
    const k = newVariantKey();
    setOpenDrafts((d) => ({ ...d, [k]: { ...EMPTY_DRAFT } }));
  }

  async function fireSamples(
    key: string,
    draft: DraftOverrides,
    nSamples: number,
  ) {
    setPendingByDraft((p) => ({ ...p, [key]: (p[key] ?? 0) + nSamples }));
    try {
      await fireApiExchangeForksPost({
        baseUrl: CLIENT_API_BASE,
        body: {
          base_exchange_id: exchangeId,
          overrides: draftToOverrides(draft),
          n_samples: nSamples,
        },
      });
      // Open-draft becomes a real variant after firing — drop it from drafts
      // since the persisted column will pick up the response.
      if (key.startsWith("new-")) {
        setOpenDrafts((d) => {
          const c = { ...d };
          delete c[key];
          return c;
        });
      }
      await queryClient.invalidateQueries({ queryKey: ["forks", exchangeId] });
    } catch (err) {
      console.error("fire fork failed", err);
    } finally {
      setPendingByDraft((p) => {
        const c = { ...p };
        c[key] = Math.max(0, (c[key] ?? 0) - nSamples);
        if (c[key] === 0) delete c[key];
        return c;
      });
    }
  }

  async function deleteVariantSamples(ids: string[]) {
    if (!confirm(`Delete ${ids.length} sample(s) in this variant?`)) return;
    for (const id of ids) {
      try {
        await deleteForkApiExchangeForksForkIdDelete({
          baseUrl: CLIENT_API_BASE,
          path: { fork_id: id },
        });
      } catch (err) {
        console.error("delete fork failed", err);
      }
    }
    await queryClient.invalidateQueries({ queryKey: ["forks", exchangeId] });
  }

  const draftKeys = Object.keys(openDrafts);

  return (
    <div className="fork-panel">
      <div className="fork-panel-bar">
        <span className="fork-panel-title">forks</span>
        <span className="fork-panel-summary">
          {forks.length} sample{forks.length === 1 ? "" : "s"}
          {variants.length > 0 ? ` · ${variants.length} config${variants.length === 1 ? "" : "s"}` : ""}
          {!base ? " · loading base..." : ""}
        </span>
        <span className="fork-panel-spacer" />
        <button type="button" className="fork-panel-close" onClick={onClose} title="close">
          ✕
        </button>
      </div>
      <div className="fork-columns">
        <OriginalColumn detail={exchangeDetail} base={base ?? null} />

        {variants.map((v) => (
          <VariantColumn
            key={v.hash}
            base={base ?? null}
            hash={v.hash}
            samples={v.samples}
            overrides={v.overrides as ForkOverrides}
            pending={pendingByDraft[v.hash] ?? 0}
            onFire={(draft, n) => fireSamples(v.hash, draft, n)}
            onDelete={() => deleteVariantSamples(v.samples.map((s) => s.id))}
          />
        ))}

        {draftKeys.map((k) => (
          <NewDraftColumn
            key={k}
            draftKey={k}
            base={base ?? null}
            initialDraft={openDrafts[k]}
            pending={pendingByDraft[k] ?? 0}
            onFire={(draft, n) => fireSamples(k, draft, n)}
            onCancel={() =>
              setOpenDrafts((d) => {
                const c = { ...d };
                delete c[k];
                return c;
              })
            }
          />
        ))}

        <NewVariantTile onClick={addNewVariant} />
      </div>
      {variants.length === 0 && draftKeys.length === 0 && (
        <div className="fork-empty">
          No forks yet. Click <span style={{ color: "#b88c2a" }}>+ new variant</span> to fork
          this exchange and fire one or more samples under different conditions.
        </div>
      )}
    </div>
  );
}

function NewVariantTile({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      className="fork-column fork-column--new"
      onClick={onClick}
    >
      <span className="fork-column--new-content">
        <span className="fork-column--new-icon">+</span>
        <span>new variant</span>
      </span>
    </button>
  );
}

function OriginalColumn({
  detail,
  base,
}: {
  detail: LlmExchangeOut;
  base: BaseExchangeOut | null;
}) {
  return (
    <div className="fork-column fork-column--original">
      <div className="fork-col-header">
        <div className="fork-col-title-row">
          <span className="fork-col-label">original</span>
          {base ? <span className="fork-col-hash">{base.exchange_id.slice(0, 8)}</span> : null}
        </div>
        <div className="fork-chips">
          <span className="fork-chip fork-chip--captured">captured</span>
          {detail.duration_ms != null && (
            <span className="fork-chip fork-chip--original">
              {(detail.duration_ms / 1000).toFixed(1)}s
            </span>
          )}
          {detail.input_tokens != null && (
            <span className="fork-chip fork-chip--original">
              {detail.input_tokens}↓ {detail.output_tokens ?? 0}↑
            </span>
          )}
        </div>
      </div>
      <div className="fork-samples">
        <div className="fork-sample fork-sample--original">
          <div className="fork-sample-meta">
            <span className="fork-sample-label">captured</span>
            {detail.input_tokens != null && (
              <span className="fork-sample-tokens">
                in/out {detail.input_tokens}/{detail.output_tokens ?? 0}
              </span>
            )}
          </div>
          {detail.error ? (
            <div className="fork-sample-error">{detail.error}</div>
          ) : (
            <>
              {detail.response_text && (
                <div className="fork-sample-text">{detail.response_text}</div>
              )}
              {detail.tool_calls && detail.tool_calls.length > 0 && (
                <ToolUseList tools={detail.tool_calls} />
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function VariantColumn({
  base,
  hash,
  samples,
  overrides,
  pending,
  onFire,
  onDelete,
}: {
  base: BaseExchangeOut | null;
  hash: string;
  samples: ForkOut[];
  overrides: ForkOverrides;
  pending: number;
  onFire: (draft: DraftOverrides, n: number) => void | Promise<void>;
  onDelete: () => void;
}) {
  const [draft, setDraft] = useState<DraftOverrides>(() => overridesToDraft(overrides));
  // If overrides change underneath us (e.g. siblings reload), reset.
  useEffect(() => {
    setDraft(overridesToDraft(overrides));
  }, [overrides]);

  const chips = useMemo(() => diffChips(draft, base), [draft, base]);
  const shortHash = hash.slice(0, 8);

  return (
    <div className="fork-column">
      <div className="fork-col-header">
        <div className="fork-col-title-row">
          <span className="fork-col-label">variant</span>
          <span className="fork-col-hash">{shortHash}</span>
          <div className="fork-col-actions">
            <button
              type="button"
              className="fork-col-delete"
              onClick={onDelete}
              title="delete all samples in this variant"
            >
              ✕
            </button>
          </div>
        </div>
        <div className="fork-chips">
          {chips.length === 0 ? (
            <span className="fork-chip fork-chip--captured">no diff</span>
          ) : (
            chips.map((c, i) => (
              <span key={i} className="fork-chip">
                {c}
              </span>
            ))
          )}
        </div>
      </div>
      <EditForm base={base} draft={draft} setDraft={setDraft} />
      <FireRow
        base={base}
        draft={draft}
        pending={pending}
        onFire={(n) => onFire(draft, n)}
      />
      <SamplesList samples={samples} pendingCount={pending} totalKnown={samples.length} />
    </div>
  );
}

function NewDraftColumn({
  draftKey: _draftKey,
  base,
  initialDraft,
  pending,
  onFire,
  onCancel,
}: {
  draftKey: string;
  base: BaseExchangeOut | null;
  initialDraft: DraftOverrides;
  pending: number;
  onFire: (draft: DraftOverrides, n: number) => void | Promise<void>;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState<DraftOverrides>(initialDraft);
  const chips = useMemo(() => diffChips(draft, base), [draft, base]);

  return (
    <div className="fork-column">
      <div className="fork-col-header">
        <div className="fork-col-title-row">
          <span className="fork-col-label">draft</span>
          <span className="fork-col-hash">unsaved</span>
          <div className="fork-col-actions">
            <button
              type="button"
              className="fork-col-delete"
              onClick={onCancel}
              title="discard this draft"
            >
              ✕
            </button>
          </div>
        </div>
        <div className="fork-chips">
          {chips.length === 0 ? (
            <span className="fork-chip fork-chip--captured">inherits all</span>
          ) : (
            chips.map((c, i) => (
              <span key={i} className="fork-chip">
                {c}
              </span>
            ))
          )}
        </div>
      </div>
      <EditForm base={base} draft={draft} setDraft={setDraft} startOpen />
      <FireRow
        base={base}
        draft={draft}
        pending={pending}
        onFire={(n) => onFire(draft, n)}
      />
      <SamplesList samples={[]} pendingCount={pending} totalKnown={0} emptyHint="will appear after firing" />
    </div>
  );
}

function EditForm({
  base,
  draft,
  setDraft,
  startOpen,
}: {
  base: BaseExchangeOut | null;
  draft: DraftOverrides;
  setDraft: React.Dispatch<React.SetStateAction<DraftOverrides>>;
  startOpen?: boolean;
}) {
  const [open, setOpen] = useState(Boolean(startOpen));
  const editedFields = useMemo(() => {
    const fields: string[] = [];
    if (draft.systemPrompt !== null) fields.push("sys");
    if (draft.messages !== null) fields.push("msgs");
    if (draft.tools !== null) fields.push("tools");
    if (draft.model !== null) fields.push("model");
    if (draft.temperature !== null) fields.push("temp");
    if (draft.maxTokens !== null) fields.push("max_tok");
    return fields;
  }, [draft]);

  return (
    <div className="fork-form">
      <button
        type="button"
        className="fork-form-toggle"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="fork-form-toggle-icon">{open ? "▾" : "▸"}</span>
        edit overrides
        {editedFields.length > 0 ? (
          <span style={{ marginLeft: 6, color: "#b88c2a" }}>· {editedFields.join(", ")}</span>
        ) : (
          <span style={{ marginLeft: 6, color: "#aaa" }}>· (none — inherits all)</span>
        )}
      </button>
      {open && (
        <div className="fork-form-body">
          <SystemPromptField base={base} draft={draft} setDraft={setDraft} />
          <MessageStackField base={base} draft={draft} setDraft={setDraft} />
          <ToolsField base={base} draft={draft} setDraft={setDraft} />
          <ModelField base={base} draft={draft} setDraft={setDraft} />
          <SamplingFields base={base} draft={draft} setDraft={setDraft} />
        </div>
      )}
    </div>
  );
}

function FieldLabel({
  modified,
  onReset,
  children,
}: {
  modified: boolean;
  onReset?: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="fork-field-label">
      <span className={modified ? "fork-field-label-mod" : undefined}>{children}</span>
      {modified && onReset && (
        <button type="button" className="fork-field-reset" onClick={onReset}>
          reset
        </button>
      )}
    </div>
  );
}

function SystemPromptField({
  base,
  draft,
  setDraft,
}: {
  base: BaseExchangeOut | null;
  draft: DraftOverrides;
  setDraft: React.Dispatch<React.SetStateAction<DraftOverrides>>;
}) {
  const value = draft.systemPrompt ?? base?.system_prompt ?? "";
  const modified = draft.systemPrompt !== null;
  return (
    <div>
      <FieldLabel
        modified={modified}
        onReset={() => setDraft((d) => ({ ...d, systemPrompt: null }))}
      >
        system prompt
      </FieldLabel>
      <textarea
        className="fork-textarea fork-textarea--system"
        value={value}
        onChange={(e) => setDraft((d) => ({ ...d, systemPrompt: e.target.value }))}
      />
    </div>
  );
}

function MessageStackField({
  base,
  draft,
  setDraft,
}: {
  base: BaseExchangeOut | null;
  draft: DraftOverrides;
  setDraft: React.Dispatch<React.SetStateAction<DraftOverrides>>;
}) {
  const baseMessages = (base?.user_messages ?? []) as Msg[];
  const value = draft.messages ?? baseMessages;
  const modified = draft.messages !== null;
  function update(next: Msg[]) {
    setDraft((d) => ({ ...d, messages: next }));
  }
  return (
    <div>
      <FieldLabel
        modified={modified}
        onReset={() => setDraft((d) => ({ ...d, messages: null }))}
      >
        message stack ({value.length})
      </FieldLabel>
      <div className="fork-msg-stack">
        {value.map((m, i) => (
          <MessageCard
            key={i}
            msg={m}
            onChange={(updated) => {
              const next = [...value];
              next[i] = updated;
              update(next);
            }}
            onDelete={() => update(value.filter((_, idx) => idx !== i))}
            onMoveUp={
              i > 0
                ? () => {
                    const next = [...value];
                    [next[i - 1], next[i]] = [next[i], next[i - 1]];
                    update(next);
                  }
                : undefined
            }
            onMoveDown={
              i < value.length - 1
                ? () => {
                    const next = [...value];
                    [next[i], next[i + 1]] = [next[i + 1], next[i]];
                    update(next);
                  }
                : undefined
            }
          />
        ))}
        <button
          type="button"
          className="fork-msg-add"
          onClick={() => update([...value, { role: "user", content: "" }])}
        >
          + message
        </button>
      </div>
    </div>
  );
}

function MessageCard({
  msg,
  onChange,
  onDelete,
  onMoveUp,
  onMoveDown,
}: {
  msg: Msg;
  onChange: (next: Msg) => void;
  onDelete: () => void;
  onMoveUp?: () => void;
  onMoveDown?: () => void;
}) {
  const isStringContent = typeof msg.content === "string";
  return (
    <div className="fork-msg-card">
      <span className="fork-msg-handle" title="move (up/down with arrow buttons)">⠿</span>
      <div className="fork-msg-card-body">
        <div className="fork-msg-card-meta">
          <select
            className="fork-msg-role-select"
            value={msg.role}
            onChange={(e) => onChange({ ...msg, role: e.target.value })}
          >
            <option value="user">user</option>
            <option value="assistant">assistant</option>
          </select>
          {!isStringContent && (
            <span className="fork-msg-card-blocks-note">
              ({Array.isArray(msg.content) ? msg.content.length : 0} block(s) — JSON below)
            </span>
          )}
          <span style={{ flex: 1 }} />
          {onMoveUp && (
            <button
              type="button"
              className="fork-tool-action-btn"
              onClick={onMoveUp}
              title="move up"
            >
              ↑
            </button>
          )}
          {onMoveDown && (
            <button
              type="button"
              className="fork-tool-action-btn"
              onClick={onMoveDown}
              title="move down"
            >
              ↓
            </button>
          )}
          <button
            type="button"
            className="fork-msg-delete"
            onClick={onDelete}
            title="delete message"
          >
            ✕
          </button>
        </div>
        {isStringContent ? (
          <textarea
            className="fork-textarea"
            value={String(msg.content ?? "")}
            onChange={(e) => onChange({ ...msg, content: e.target.value })}
          />
        ) : (
          <BlockJsonEditor
            value={msg.content}
            onChange={(parsed) => onChange({ ...msg, content: parsed })}
          />
        )}
      </div>
    </div>
  );
}

function BlockJsonEditor({
  value,
  onChange,
}: {
  value: unknown;
  onChange: (next: unknown) => void;
}) {
  const [text, setText] = useState(JSON.stringify(value ?? [], null, 2));
  const [err, setErr] = useState<string | null>(null);
  return (
    <div>
      <textarea
        className="fork-textarea fork-textarea--code"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={() => {
          try {
            const parsed = JSON.parse(text);
            setErr(null);
            onChange(parsed);
          } catch (e) {
            setErr(e instanceof Error ? e.message : String(e));
          }
        }}
      />
      {err && (
        <div style={{ fontSize: 10, color: "#c44", marginTop: 2 }}>JSON parse error: {err}</div>
      )}
    </div>
  );
}

function ToolsField({
  base,
  draft,
  setDraft,
}: {
  base: BaseExchangeOut | null;
  draft: DraftOverrides;
  setDraft: React.Dispatch<React.SetStateAction<DraftOverrides>>;
}) {
  const baseTools = (base?.tools ?? []) as Tool[];
  const value = draft.tools ?? baseTools;
  const modified = draft.tools !== null;
  const baseNames = useMemo(() => new Set(baseTools.map((t) => t.name)), [baseTools]);

  function update(next: Tool[]) {
    setDraft((d) => ({ ...d, tools: next }));
  }
  function setEnabled(toolName: string, enabled: boolean) {
    const present = value.find((t) => t.name === toolName);
    if (enabled && !present) {
      const fromBase = baseTools.find((t) => t.name === toolName);
      if (fromBase) update([...value, fromBase]);
    } else if (!enabled && present) {
      update(value.filter((t) => t.name !== toolName));
    }
  }

  // Show the union of base tools + tools added by the draft, marking
  // which are currently enabled and which were added (not in base).
  const allNames = useMemo(() => {
    const set = new Set<string>();
    baseTools.forEach((t) => set.add(t.name));
    value.forEach((t) => set.add(t.name));
    return [...set];
  }, [baseTools, value]);

  return (
    <div>
      <FieldLabel
        modified={modified}
        onReset={() => setDraft((d) => ({ ...d, tools: null }))}
      >
        tools ({value.length}{baseTools.length !== value.length ? ` / was ${baseTools.length}` : ""})
      </FieldLabel>
      <div className="fork-tools-list">
        {allNames.map((name) => {
          const inDraft = value.find((t) => t.name === name);
          const inBase = baseTools.find((t) => t.name === name);
          const display = inDraft ?? inBase!;
          const enabled = Boolean(inDraft);
          const isAdded = !baseNames.has(name);
          return (
            <ToolCard
              key={name}
              tool={display}
              enabled={enabled}
              isAdded={isAdded}
              onToggle={(en) => setEnabled(name, en)}
              onEdit={(updated) => {
                if (!enabled) return;
                update(value.map((t) => (t.name === name ? updated : t)));
              }}
              onRemoveAdded={() => update(value.filter((t) => t.name !== name))}
            />
          );
        })}
        <AddToolControl
          onAdd={(tool) => {
            if (value.find((t) => t.name === tool.name)) return;
            update([...value, tool]);
          }}
        />
      </div>
    </div>
  );
}

function ToolCard({
  tool,
  enabled,
  isAdded,
  onToggle,
  onEdit,
  onRemoveAdded,
}: {
  tool: Tool;
  enabled: boolean;
  isAdded: boolean;
  onToggle: (enabled: boolean) => void;
  onEdit: (updated: Tool) => void;
  onRemoveAdded: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [descDraft, setDescDraft] = useState(tool.description);
  const [schemaText, setSchemaText] = useState(JSON.stringify(tool.input_schema, null, 2));
  const [schemaErr, setSchemaErr] = useState<string | null>(null);

  return (
    <div
      className={`fork-tool-card${enabled ? "" : " fork-tool-card--off"}${isAdded ? " fork-tool-card--added" : ""}`}
    >
      <div className="fork-tool-row">
        <input
          type="checkbox"
          className="fork-tool-toggle"
          checked={enabled}
          onChange={(e) => onToggle(e.target.checked)}
          title={enabled ? "disable this tool" : "enable this tool"}
        />
        <span className="fork-tool-name">{tool.name}</span>
        {isAdded && <span className="fork-tool-mark">added</span>}
        <span className="fork-tool-desc">{tool.description}</span>
        <div className="fork-tool-actions">
          <button
            type="button"
            className="fork-tool-action-btn"
            onClick={() => setEditing((v) => !v)}
            disabled={!enabled}
          >
            {editing ? "done" : "edit"}
          </button>
          {isAdded && (
            <button
              type="button"
              className="fork-tool-action-btn fork-tool-action-btn--danger"
              onClick={onRemoveAdded}
            >
              remove
            </button>
          )}
        </div>
      </div>
      {editing && enabled && (
        <div className="fork-tool-edit-body">
          <FieldLabel modified={false}>description</FieldLabel>
          <textarea
            className="fork-textarea fork-textarea--code"
            value={descDraft}
            onChange={(e) => setDescDraft(e.target.value)}
            onBlur={() => onEdit({ ...tool, description: descDraft })}
            rows={2}
          />
          <div style={{ height: 6 }} />
          <FieldLabel modified={false}>input_schema (JSON)</FieldLabel>
          <textarea
            className="fork-textarea fork-textarea--code"
            value={schemaText}
            onChange={(e) => setSchemaText(e.target.value)}
            onBlur={() => {
              try {
                const parsed = JSON.parse(schemaText);
                setSchemaErr(null);
                onEdit({ ...tool, description: descDraft, input_schema: parsed });
              } catch (e) {
                setSchemaErr(e instanceof Error ? e.message : String(e));
              }
            }}
            rows={6}
          />
          {schemaErr && (
            <div style={{ fontSize: 10, color: "#c44", marginTop: 2 }}>{schemaErr}</div>
          )}
        </div>
      )}
    </div>
  );
}

function AddToolControl({ onAdd }: { onAdd: (tool: Tool) => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [schemaText, setSchemaText] = useState('{\n  "type": "object",\n  "properties": {}\n}');
  const [err, setErr] = useState<string | null>(null);

  if (!open) {
    return (
      <button type="button" className="fork-tools-add" onClick={() => setOpen(true)}>
        + add tool
      </button>
    );
  }
  return (
    <div className="fork-tool-card fork-tool-card--added">
      <div className="fork-tool-edit-body">
        <FieldLabel modified={true}>new tool · name</FieldLabel>
        <input
          className="fork-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="tool_name"
        />
        <div style={{ height: 6 }} />
        <FieldLabel modified={true}>description</FieldLabel>
        <textarea
          className="fork-textarea fork-textarea--code"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={2}
        />
        <div style={{ height: 6 }} />
        <FieldLabel modified={true}>input_schema (JSON)</FieldLabel>
        <textarea
          className="fork-textarea fork-textarea--code"
          value={schemaText}
          onChange={(e) => setSchemaText(e.target.value)}
          rows={6}
        />
        {err && <div style={{ fontSize: 10, color: "#c44", marginTop: 2 }}>{err}</div>}
        <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
          <button
            type="button"
            className="fork-fire-confirm-btn fork-fire-confirm-btn--ok"
            onClick={() => {
              try {
                const parsed = JSON.parse(schemaText);
                if (!name.trim()) {
                  setErr("name is required");
                  return;
                }
                onAdd({ name: name.trim(), description, input_schema: parsed });
                setOpen(false);
                setName("");
                setDescription("");
                setErr(null);
              } catch (e) {
                setErr(e instanceof Error ? e.message : String(e));
              }
            }}
          >
            add
          </button>
          <button
            type="button"
            className="fork-fire-confirm-btn"
            onClick={() => {
              setOpen(false);
              setErr(null);
            }}
          >
            cancel
          </button>
        </div>
      </div>
    </div>
  );
}

function ModelField({
  base,
  draft,
  setDraft,
}: {
  base: BaseExchangeOut | null;
  draft: DraftOverrides;
  setDraft: React.Dispatch<React.SetStateAction<DraftOverrides>>;
}) {
  const value = draft.model ?? base?.model ?? MODELS[0];
  const modified = draft.model !== null;
  const options = MODELS.includes(value) ? MODELS : [value, ...MODELS];
  return (
    <div>
      <FieldLabel
        modified={modified}
        onReset={() => setDraft((d) => ({ ...d, model: null, temperature: null }))}
      >
        model
      </FieldLabel>
      <select
        className="fork-select"
        value={value}
        onChange={(e) =>
          setDraft((d) => ({
            ...d,
            model: e.target.value,
            // If switching to a model that doesn't support sampling, drop temp.
            temperature: modelSupportsSampling(e.target.value) ? d.temperature : null,
          }))
        }
      >
        {options.map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
      </select>
    </div>
  );
}

function SamplingFields({
  base,
  draft,
  setDraft,
}: {
  base: BaseExchangeOut | null;
  draft: DraftOverrides;
  setDraft: React.Dispatch<React.SetStateAction<DraftOverrides>>;
}) {
  const effectiveModel = draft.model ?? base?.model ?? MODELS[0];
  const supportsSampling = modelSupportsSampling(effectiveModel);
  const tempValue = draft.temperature ?? base?.temperature ?? 0.15;
  const tempModified = draft.temperature !== null;
  const maxTok = draft.maxTokens ?? base?.max_tokens ?? 20000;
  const maxTokModified = draft.maxTokens !== null;
  return (
    <>
      <div>
        <FieldLabel
          modified={tempModified}
          onReset={() => setDraft((d) => ({ ...d, temperature: null }))}
        >
          temperature
        </FieldLabel>
        {supportsSampling ? (
          <div className="fork-field-row">
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={tempValue}
              className="fork-temp-slider"
              onChange={(e) =>
                setDraft((d) => ({ ...d, temperature: parseFloat(e.target.value) }))
              }
            />
            <span className="fork-temp-display">{tempValue.toFixed(2)}</span>
          </div>
        ) : (
          <span className="fork-temp-disabled">
            {effectiveModel} doesn&apos;t accept sampling params (adaptive thinking on)
          </span>
        )}
      </div>
      <div>
        <FieldLabel
          modified={maxTokModified}
          onReset={() => setDraft((d) => ({ ...d, maxTokens: null }))}
        >
          max_tokens
        </FieldLabel>
        <input
          type="number"
          className="fork-input"
          value={maxTok}
          min={1}
          step={500}
          onChange={(e) => setDraft((d) => ({ ...d, maxTokens: parseInt(e.target.value, 10) }))}
        />
      </div>
    </>
  );
}

function FireRow({
  base,
  draft,
  pending,
  onFire,
}: {
  base: BaseExchangeOut | null;
  draft: DraftOverrides;
  pending: number;
  onFire: (n: number) => void | Promise<void>;
}) {
  const [n, setN] = useState(1);
  const [confirming, setConfirming] = useState(false);
  const effectiveModel = draft.model ?? base?.model ?? MODELS[0];

  // Rough cost estimate. Use the original exchange's input_tokens as the
  // rough input size; assume 800 expected output tokens per sample.
  const inputTokensRough = 0; // unknown until we have a baseline; conservative
  const expectedOutput = 800;
  const est = useMemo(
    () => estimateCost(effectiveModel, Math.max(inputTokensRough, 4000), expectedOutput, n),
    [effectiveModel, inputTokensRough, n],
  );
  const high = est > 1.0;
  const fireBlocked = pending > 0 || !base;

  function fire() {
    if (high && !confirming) {
      setConfirming(true);
      return;
    }
    setConfirming(false);
    onFire(n);
  }

  return (
    <div>
      <div className="fork-fire-row">
        <span style={{ fontFamily: "var(--font-geist-mono), monospace", fontSize: 11, color: "#6a5610" }}>
          sample
        </span>
        <input
          className="fork-fire-input"
          type="number"
          value={n}
          min={1}
          max={20}
          onChange={(e) => setN(Math.max(1, Math.min(20, parseInt(e.target.value, 10) || 1)))}
        />
        <button
          type="button"
          className="fork-fire-btn"
          onClick={fire}
          disabled={fireBlocked}
        >
          {pending > 0 ? `firing ${pending}…` : "fire"}
        </button>
        <span className={`fork-fire-est${high ? " fork-fire-est--high" : ""}`}>
          ~${est.toFixed(3)}
        </span>
      </div>
      {confirming && (
        <div className="fork-fire-confirm" style={{ margin: "8px 12px" }}>
          <span>
            estimate over $1 — confirm fire of {n} sample{n === 1 ? "" : "s"}?
          </span>
          <span style={{ flex: 1 }} />
          <button
            type="button"
            className="fork-fire-confirm-btn fork-fire-confirm-btn--ok"
            onClick={fire}
          >
            yes, fire
          </button>
          <button
            type="button"
            className="fork-fire-confirm-btn"
            onClick={() => setConfirming(false)}
          >
            cancel
          </button>
        </div>
      )}
    </div>
  );
}

function SamplesList({
  samples,
  pendingCount,
  totalKnown,
  emptyHint,
}: {
  samples: ForkOut[];
  pendingCount: number;
  totalKnown: number;
  emptyHint?: string;
}) {
  const total = totalKnown + pendingCount;
  return (
    <div className="fork-samples">
      {samples.map((s, i) => (
        <SampleCard key={s.id} sample={s} indexLabel={`${i + 1}/${total}`} />
      ))}
      {Array.from({ length: pendingCount }).map((_, i) => (
        <div className="fork-skeleton" key={`pending-${i}`} />
      ))}
      {samples.length === 0 && pendingCount === 0 && (
        <div className="fork-empty">{emptyHint ?? "no samples yet"}</div>
      )}
    </div>
  );
}

function SampleCard({ sample, indexLabel }: { sample: ForkOut; indexLabel: string }) {
  return (
    <div className="fork-sample">
      <div className="fork-sample-meta">
        <span className="fork-sample-label">sample {indexLabel}</span>
        {sample.input_tokens != null && (
          <span className="fork-sample-tokens">
            {sample.input_tokens}↓ {sample.output_tokens ?? 0}↑
          </span>
        )}
        {sample.duration_ms != null && (
          <span className="fork-sample-tokens">{(sample.duration_ms / 1000).toFixed(1)}s</span>
        )}
        <span className="fork-sample-cost">${(sample.cost_usd ?? 0).toFixed(4)}</span>
      </div>
      {sample.error ? (
        <div className="fork-sample-error">{sample.error}</div>
      ) : (
        <>
          {sample.response_text && <div className="fork-sample-text">{sample.response_text}</div>}
          {sample.tool_calls && sample.tool_calls.length > 0 && (
            <ToolUseList tools={sample.tool_calls} />
          )}
        </>
      )}
    </div>
  );
}

function ToolUseList({ tools }: { tools: Array<{ [k: string]: unknown }> }) {
  return (
    <div className="fork-tool-uses">
      {tools.map((t, i) => {
        const name = (t.name as string | undefined) ?? "(unknown)";
        const input = t.input;
        return (
          <div className="fork-tool-use" key={i}>
            <span className="fork-tool-use-name">{name}</span>
            {input !== undefined && `: ${JSON.stringify(input)}`}
          </div>
        );
      })}
    </div>
  );
}
