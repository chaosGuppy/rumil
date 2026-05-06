"use client";
import { useState, useMemo } from "react";

type Item = { searchKey: string; node: React.ReactNode };

export function Filter({
  items,
  placeholder,
}: {
  items: Item[];
  placeholder?: string;
}) {
  const [q, setQ] = useState("");
  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return items;
    return items.filter((it) => it.searchKey.toLowerCase().includes(needle));
  }, [q, items]);

  return (
    <>
      <div className="atlas-filter">
        <input
          type="text"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={placeholder ?? "filter…"}
          autoComplete="off"
          spellCheck={false}
        />
        <span className="atlas-filter-meta">
          {filtered.length} / {items.length}
        </span>
      </div>
      <div>{filtered.map((it, i) => <div key={i}>{it.node}</div>)}</div>
    </>
  );
}
