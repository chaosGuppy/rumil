"use client";

import { useState } from "react";
import type { Msg } from "./types";
import { ROLE_OPTIONS } from "./types";

// Anthropic content blocks come in a few flavors. We handle the three
// the model API actually round-trips: text, tool_use, tool_result. Any
// unrecognized shape falls through to a raw JSON editor so nothing is
// silently mangled.
type BlockKind = "text" | "tool_use" | "tool_result" | "other";

function classifyBlock(b: unknown): BlockKind {
  if (!b || typeof b !== "object") return "other";
  const t = (b as { type?: unknown }).type;
  if (t === "text") return "text";
  if (t === "tool_use") return "tool_use";
  if (t === "tool_result") return "tool_result";
  return "other";
}

export function MessagesEditor({
  value,
  onChange,
}: {
  value: Msg[];
  onChange: (next: Msg[]) => void;
}) {
  function update(idx: number, next: Msg) {
    const out = [...value];
    out[idx] = next;
    onChange(out);
  }
  function remove(idx: number) {
    onChange(value.filter((_, i) => i !== idx));
  }
  function move(idx: number, dir: -1 | 1) {
    const j = idx + dir;
    if (j < 0 || j >= value.length) return;
    const out = [...value];
    [out[idx], out[j]] = [out[j], out[idx]];
    onChange(out);
  }
  function add() {
    onChange([...value, { role: "user", content: "" }]);
  }

  return (
    <div className="atlas-pg-msgs">
      {value.length === 0 && (
        <div className="atlas-pg-msgs-empty">no messages — add one to start</div>
      )}
      {value.map((m, i) => (
        <MessageCard
          key={i}
          index={i}
          msg={m}
          onChange={(next) => update(i, next)}
          onRemove={() => remove(i)}
          onMoveUp={i > 0 ? () => move(i, -1) : undefined}
          onMoveDown={i < value.length - 1 ? () => move(i, 1) : undefined}
        />
      ))}
      <button type="button" className="atlas-pg-msg-add" onClick={add}>
        + add message
      </button>
    </div>
  );
}

function MessageCard({
  index,
  msg,
  onChange,
  onRemove,
  onMoveUp,
  onMoveDown,
}: {
  index: number;
  msg: Msg;
  onChange: (next: Msg) => void;
  onRemove: () => void;
  onMoveUp?: () => void;
  onMoveDown?: () => void;
}) {
  const isString = typeof msg.content === "string";
  const blocks = Array.isArray(msg.content) ? msg.content : null;

  return (
    <div className="atlas-pg-msg">
      <div className="atlas-pg-msg-head">
        <span className="atlas-pg-msg-idx">#{index}</span>
        <select
          className="atlas-pg-msg-role"
          value={msg.role}
          onChange={(e) => onChange({ ...msg, role: e.target.value })}
        >
          {ROLE_OPTIONS.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
          {!ROLE_OPTIONS.includes(msg.role as (typeof ROLE_OPTIONS)[number]) && (
            <option value={msg.role}>{msg.role}</option>
          )}
        </select>
        <span className="atlas-pg-msg-shape">
          {isString
            ? "string content"
            : blocks
              ? `${blocks.length} block${blocks.length === 1 ? "" : "s"}`
              : "raw"}
        </span>
        <span className="atlas-pg-msg-grow" />
        {onMoveUp && (
          <button
            type="button"
            className="atlas-pg-msg-mini"
            onClick={onMoveUp}
            title="move up"
          >
            ↑
          </button>
        )}
        {onMoveDown && (
          <button
            type="button"
            className="atlas-pg-msg-mini"
            onClick={onMoveDown}
            title="move down"
          >
            ↓
          </button>
        )}
        {isString ? (
          <button
            type="button"
            className="atlas-pg-msg-mini"
            onClick={() =>
              onChange({
                ...msg,
                content: [{ type: "text", text: String(msg.content ?? "") }],
              })
            }
            title="convert to block array"
          >
            blocks
          </button>
        ) : (
          <button
            type="button"
            className="atlas-pg-msg-mini"
            onClick={() => {
              const flat = (Array.isArray(msg.content) ? msg.content : [])
                .map((b) =>
                  b && typeof b === "object" && (b as { type?: unknown }).type === "text"
                    ? String((b as { text?: unknown }).text ?? "")
                    : "",
                )
                .filter(Boolean)
                .join("\n\n");
              onChange({ ...msg, content: flat });
            }}
            title="flatten to string (text blocks only)"
          >
            string
          </button>
        )}
        <button
          type="button"
          className="atlas-pg-msg-mini is-danger"
          onClick={onRemove}
          title="delete"
        >
          ✕
        </button>
      </div>
      <div className="atlas-pg-msg-body">
        {isString ? (
          <textarea
            className="atlas-pg-textarea"
            value={String(msg.content ?? "")}
            onChange={(e) => onChange({ ...msg, content: e.target.value })}
            spellCheck={false}
          />
        ) : blocks ? (
          <BlocksEditor
            blocks={blocks}
            onChange={(next) => onChange({ ...msg, content: next })}
          />
        ) : (
          <JsonField
            value={msg.content}
            onChange={(parsed) => onChange({ ...msg, content: parsed })}
            rows={6}
          />
        )}
      </div>
    </div>
  );
}

function BlocksEditor({
  blocks,
  onChange,
}: {
  blocks: unknown[];
  onChange: (next: unknown[]) => void;
}) {
  function update(idx: number, next: unknown) {
    const out = [...blocks];
    out[idx] = next;
    onChange(out);
  }
  function remove(idx: number) {
    onChange(blocks.filter((_, i) => i !== idx));
  }
  function move(idx: number, dir: -1 | 1) {
    const j = idx + dir;
    if (j < 0 || j >= blocks.length) return;
    const out = [...blocks];
    [out[idx], out[j]] = [out[j], out[idx]];
    onChange(out);
  }
  function add(kind: BlockKind) {
    if (kind === "text") {
      onChange([...blocks, { type: "text", text: "" }]);
    } else if (kind === "tool_use") {
      onChange([
        ...blocks,
        { type: "tool_use", id: `toolu_${Math.random().toString(36).slice(2, 10)}`, name: "", input: {} },
      ]);
    } else if (kind === "tool_result") {
      onChange([
        ...blocks,
        { type: "tool_result", tool_use_id: "", content: "" },
      ]);
    }
  }

  return (
    <div className="atlas-pg-blocks">
      {blocks.map((b, i) => (
        <BlockCard
          key={i}
          index={i}
          block={b}
          onChange={(next) => update(i, next)}
          onRemove={() => remove(i)}
          onMoveUp={i > 0 ? () => move(i, -1) : undefined}
          onMoveDown={i < blocks.length - 1 ? () => move(i, 1) : undefined}
        />
      ))}
      <div className="atlas-pg-block-add-row">
        <button
          type="button"
          className="atlas-pg-msg-mini"
          onClick={() => add("text")}
        >
          + text
        </button>
        <button
          type="button"
          className="atlas-pg-msg-mini"
          onClick={() => add("tool_use")}
        >
          + tool_use
        </button>
        <button
          type="button"
          className="atlas-pg-msg-mini"
          onClick={() => add("tool_result")}
        >
          + tool_result
        </button>
      </div>
    </div>
  );
}

function BlockCard({
  index,
  block,
  onChange,
  onRemove,
  onMoveUp,
  onMoveDown,
}: {
  index: number;
  block: unknown;
  onChange: (next: unknown) => void;
  onRemove: () => void;
  onMoveUp?: () => void;
  onMoveDown?: () => void;
}) {
  const kind = classifyBlock(block);

  return (
    <div className="atlas-pg-block">
      <div className="atlas-pg-block-head">
        <span className="atlas-pg-block-tag">[{index}]</span>
        <span className={`atlas-pg-block-kind is-${kind}`}>{kind}</span>
        <span className="atlas-pg-msg-grow" />
        {onMoveUp && (
          <button
            type="button"
            className="atlas-pg-msg-mini"
            onClick={onMoveUp}
            title="move up"
          >
            ↑
          </button>
        )}
        {onMoveDown && (
          <button
            type="button"
            className="atlas-pg-msg-mini"
            onClick={onMoveDown}
            title="move down"
          >
            ↓
          </button>
        )}
        <button
          type="button"
          className="atlas-pg-msg-mini is-danger"
          onClick={onRemove}
          title="delete block"
        >
          ✕
        </button>
      </div>
      <div className="atlas-pg-block-body">
        {kind === "text" && (
          <TextBlockEditor block={block as { text?: string }} onChange={onChange} />
        )}
        {kind === "tool_use" && (
          <ToolUseBlockEditor block={block as Record<string, unknown>} onChange={onChange} />
        )}
        {kind === "tool_result" && (
          <ToolResultBlockEditor
            block={block as Record<string, unknown>}
            onChange={onChange}
          />
        )}
        {kind === "other" && (
          <JsonField value={block} onChange={onChange} rows={6} />
        )}
      </div>
    </div>
  );
}

function TextBlockEditor({
  block,
  onChange,
}: {
  block: { text?: string };
  onChange: (next: unknown) => void;
}) {
  return (
    <textarea
      className="atlas-pg-textarea is-block"
      value={String(block.text ?? "")}
      onChange={(e) => onChange({ ...block, type: "text", text: e.target.value })}
      spellCheck={false}
    />
  );
}

function ToolUseBlockEditor({
  block,
  onChange,
}: {
  block: Record<string, unknown>;
  onChange: (next: unknown) => void;
}) {
  return (
    <div className="atlas-pg-tu">
      <label className="atlas-pg-inline">
        <span>id</span>
        <input
          className="atlas-pg-mono-input"
          value={String(block.id ?? "")}
          onChange={(e) => onChange({ ...block, type: "tool_use", id: e.target.value })}
        />
      </label>
      <label className="atlas-pg-inline">
        <span>name</span>
        <input
          className="atlas-pg-mono-input"
          value={String(block.name ?? "")}
          onChange={(e) =>
            onChange({ ...block, type: "tool_use", name: e.target.value })
          }
        />
      </label>
      <div className="atlas-pg-inline-block">
        <span className="atlas-pg-inline-label">input (JSON)</span>
        <JsonField
          value={block.input ?? {}}
          onChange={(parsed) =>
            onChange({ ...block, type: "tool_use", input: parsed })
          }
          rows={6}
        />
      </div>
    </div>
  );
}

function ToolResultBlockEditor({
  block,
  onChange,
}: {
  block: Record<string, unknown>;
  onChange: (next: unknown) => void;
}) {
  const content = block.content;
  const contentIsString = typeof content === "string";
  return (
    <div className="atlas-pg-tu">
      <label className="atlas-pg-inline">
        <span>tool_use_id</span>
        <input
          className="atlas-pg-mono-input"
          value={String(block.tool_use_id ?? "")}
          onChange={(e) =>
            onChange({
              ...block,
              type: "tool_result",
              tool_use_id: e.target.value,
            })
          }
        />
      </label>
      <label className="atlas-pg-inline is-row">
        <input
          type="checkbox"
          checked={Boolean(block.is_error)}
          onChange={(e) =>
            onChange({
              ...block,
              type: "tool_result",
              is_error: e.target.checked,
            })
          }
        />
        <span>is_error</span>
      </label>
      <div className="atlas-pg-inline-block">
        <span className="atlas-pg-inline-label">
          content {contentIsString ? "(string)" : "(JSON)"}
        </span>
        {contentIsString ? (
          <textarea
            className="atlas-pg-textarea is-block"
            value={String(content ?? "")}
            onChange={(e) =>
              onChange({
                ...block,
                type: "tool_result",
                content: e.target.value,
              })
            }
            spellCheck={false}
          />
        ) : (
          <JsonField
            value={content}
            onChange={(parsed) =>
              onChange({ ...block, type: "tool_result", content: parsed })
            }
            rows={6}
          />
        )}
        <button
          type="button"
          className="atlas-pg-msg-mini"
          onClick={() =>
            onChange({
              ...block,
              type: "tool_result",
              content: contentIsString ? [] : String(content ?? ""),
            })
          }
        >
          {contentIsString ? "→ JSON" : "→ string"}
        </button>
      </div>
    </div>
  );
}

// JSON field with on-blur parsing so users can type freely without
// losing focus on every keystroke. Errors surface inline; the upstream
// value is only updated when the JSON parses cleanly.
export function JsonField({
  value,
  onChange,
  rows = 6,
}: {
  value: unknown;
  onChange: (parsed: unknown) => void;
  rows?: number;
}) {
  const [text, setText] = useState(() => {
    try {
      return JSON.stringify(value ?? null, null, 2);
    } catch {
      return "null";
    }
  });
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="atlas-pg-json">
      <textarea
        className="atlas-pg-textarea is-code"
        value={text}
        rows={rows}
        spellCheck={false}
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
      {err && <div className="atlas-pg-json-err">JSON: {err}</div>}
    </div>
  );
}
