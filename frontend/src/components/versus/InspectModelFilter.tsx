"use client";

import { useEffect, useState } from "react";

/**
 * Client-side filter that hides/shows DOM nodes tagged with
 * `data-model="<id>"`. Mirrors the static self-vs-human HTML viewer:
 * "all models" shows everything; picking a specific model hides any
 * `[data-filterable]` element whose `data-model` doesn't match.
 *
 * `human` is special — it always stays visible so the held-out
 * remainder is on screen alongside whichever model is selected.
 */
export function InspectModelFilter({
  options,
}: {
  options: { value: string; label: string }[];
}) {
  const [model, setModel] = useState<string>("all");

  useEffect(() => {
    const nodes = document.querySelectorAll<HTMLElement>("[data-filterable]");
    nodes.forEach((n) => {
      const ms = (n.dataset.model ?? "").split(/\s+/).filter(Boolean);
      const stick = n.dataset.alwaysShow === "1";
      const show = stick || model === "all" || ms.includes(model);
      n.style.display = show ? "" : "none";
    });
  }, [model, options]);

  return (
    <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
      <span className="versus-muted" style={{ fontSize: 13 }}>model</span>
      <select
        value={model}
        onChange={(e) => setModel(e.target.value)}
        className="versus-select"
        style={{ padding: "4px 8px", fontSize: 13 }}
      >
        <option value="all">all models</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}
