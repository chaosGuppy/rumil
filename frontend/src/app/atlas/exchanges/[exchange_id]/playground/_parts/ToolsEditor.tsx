"use client";

import { useMemo, useState } from "react";
import type { Tool } from "./types";
import { JsonField } from "./MessagesEditor";

// Tools field. Atlas treatment: a flat dense list, each tool a single
// row that expands to reveal description + input_schema editing. Toggle
// disables a base tool (omits it from the override array); explicit
// "+ tool" adds a new one.

export function ToolsEditor({
  value,
  baseTools,
  onChange,
}: {
  value: Tool[];
  baseTools: Tool[];
  onChange: (next: Tool[]) => void;
}) {
  const baseNames = useMemo(() => new Set(baseTools.map((t) => t.name)), [baseTools]);
  const draftNames = useMemo(() => new Set(value.map((t) => t.name)), [value]);

  const allNames = useMemo(() => {
    const names: string[] = [];
    const seen = new Set<string>();
    for (const t of baseTools) {
      if (!seen.has(t.name)) {
        names.push(t.name);
        seen.add(t.name);
      }
    }
    for (const t of value) {
      if (!seen.has(t.name)) {
        names.push(t.name);
        seen.add(t.name);
      }
    }
    return names;
  }, [baseTools, value]);

  function setEnabled(name: string, enabled: boolean) {
    const present = value.find((t) => t.name === name);
    if (enabled && !present) {
      const fromBase = baseTools.find((t) => t.name === name);
      if (fromBase) onChange([...value, fromBase]);
    } else if (!enabled && present) {
      onChange(value.filter((t) => t.name !== name));
    }
  }

  function updateTool(name: string, next: Tool) {
    onChange(value.map((t) => (t.name === name ? next : t)));
  }

  function removeAdded(name: string) {
    onChange(value.filter((t) => t.name !== name));
  }

  function add(t: Tool) {
    if (draftNames.has(t.name)) return;
    onChange([...value, t]);
  }

  return (
    <div className="atlas-pg-tools">
      {allNames.length === 0 && (
        <div className="atlas-pg-msgs-empty">no tools — base exchange had none</div>
      )}
      {allNames.map((name) => {
        const inDraft = value.find((t) => t.name === name);
        const inBase = baseTools.find((t) => t.name === name);
        const display = inDraft ?? inBase!;
        const enabled = Boolean(inDraft);
        const isAdded = !baseNames.has(name);
        return (
          <ToolRow
            key={name}
            tool={display}
            enabled={enabled}
            isAdded={isAdded}
            onToggle={(en) => setEnabled(name, en)}
            onEdit={(updated) => {
              if (!enabled) return;
              updateTool(name, updated);
            }}
            onRemoveAdded={() => removeAdded(name)}
          />
        );
      })}
      <AddToolControl onAdd={add} existingNames={draftNames} />
    </div>
  );
}

function ToolRow({
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
  const [open, setOpen] = useState(false);

  return (
    <div
      className={`atlas-pg-tool${enabled ? "" : " is-off"}${isAdded ? " is-added" : ""}`}
    >
      <div className="atlas-pg-tool-row">
        <input
          type="checkbox"
          className="atlas-pg-tool-toggle"
          checked={enabled}
          onChange={(e) => onToggle(e.target.checked)}
          title={enabled ? "disable this tool" : "enable this tool"}
        />
        <span className="atlas-pg-tool-name">{tool.name || "(unnamed)"}</span>
        {isAdded && <span className="atlas-pg-tool-mark">added</span>}
        <span className="atlas-pg-tool-desc" title={tool.description}>
          {tool.description}
        </span>
        <div className="atlas-pg-tool-actions">
          <button
            type="button"
            className="atlas-pg-msg-mini"
            onClick={() => setOpen((v) => !v)}
            disabled={!enabled}
          >
            {open ? "close" : "edit"}
          </button>
          {isAdded && (
            <button
              type="button"
              className="atlas-pg-msg-mini is-danger"
              onClick={onRemoveAdded}
            >
              remove
            </button>
          )}
        </div>
      </div>
      {open && enabled && (
        <div className="atlas-pg-tool-body">
          <label className="atlas-pg-inline">
            <span>name</span>
            <input
              className="atlas-pg-mono-input"
              value={tool.name}
              onChange={(e) => onEdit({ ...tool, name: e.target.value })}
            />
          </label>
          <label className="atlas-pg-inline-block">
            <span className="atlas-pg-inline-label">description</span>
            <textarea
              className="atlas-pg-textarea is-code"
              rows={3}
              value={tool.description}
              onChange={(e) => onEdit({ ...tool, description: e.target.value })}
              spellCheck={false}
            />
          </label>
          <div className="atlas-pg-inline-block">
            <span className="atlas-pg-inline-label">input_schema (JSON)</span>
            <JsonField
              value={tool.input_schema}
              onChange={(parsed) => onEdit({ ...tool, input_schema: parsed })}
              rows={8}
            />
          </div>
        </div>
      )}
    </div>
  );
}

function AddToolControl({
  onAdd,
  existingNames,
}: {
  onAdd: (t: Tool) => void;
  existingNames: Set<string>;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [schemaText, setSchemaText] = useState(
    '{\n  "type": "object",\n  "properties": {}\n}',
  );
  const [err, setErr] = useState<string | null>(null);

  if (!open) {
    return (
      <button
        type="button"
        className="atlas-pg-tool-add"
        onClick={() => setOpen(true)}
      >
        + add tool
      </button>
    );
  }

  return (
    <div className="atlas-pg-tool is-added">
      <div className="atlas-pg-tool-body">
        <label className="atlas-pg-inline">
          <span>name</span>
          <input
            className="atlas-pg-mono-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="tool_name"
          />
        </label>
        <label className="atlas-pg-inline-block">
          <span className="atlas-pg-inline-label">description</span>
          <textarea
            className="atlas-pg-textarea is-code"
            rows={3}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            spellCheck={false}
          />
        </label>
        <label className="atlas-pg-inline-block">
          <span className="atlas-pg-inline-label">input_schema (JSON)</span>
          <textarea
            className="atlas-pg-textarea is-code"
            rows={6}
            value={schemaText}
            onChange={(e) => setSchemaText(e.target.value)}
            spellCheck={false}
          />
        </label>
        {err && <div className="atlas-pg-json-err">{err}</div>}
        <div className="atlas-pg-tool-add-actions">
          <button
            type="button"
            className="atlas-pg-msg-mini is-primary"
            onClick={() => {
              if (!name.trim()) {
                setErr("name is required");
                return;
              }
              if (existingNames.has(name.trim())) {
                setErr(`a tool named "${name.trim()}" already exists`);
                return;
              }
              try {
                const parsed = JSON.parse(schemaText);
                onAdd({
                  name: name.trim(),
                  description,
                  input_schema: parsed,
                });
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
            className="atlas-pg-msg-mini"
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
