"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";

export function DiffPicker({ a, b }: { a?: string; b?: string }) {
  const router = useRouter();
  const [aVal, setAVal] = useState(a ?? "");
  const [bVal, setBVal] = useState(b ?? "");

  function go() {
    const qs = new URLSearchParams();
    if (aVal) qs.set("a", aVal.trim());
    if (bVal) qs.set("b", bVal.trim());
    router.push(`/atlas/runs/diff${qs.toString() ? `?${qs.toString()}` : ""}`);
  }

  function swap() {
    setAVal(bVal);
    setBVal(aVal);
  }

  return (
    <div className="atlas-diff-picker">
      <span className="atlas-section-meta">a</span>
      <input
        value={aVal}
        onChange={(e) => setAVal(e.target.value)}
        placeholder="run_id"
        spellCheck={false}
      />
      <button type="button" onClick={swap} title="swap a/b">
        ↔
      </button>
      <span className="atlas-section-meta">b</span>
      <input
        value={bVal}
        onChange={(e) => setBVal(e.target.value)}
        placeholder="run_id"
        spellCheck={false}
      />
      <button type="button" onClick={go}>
        compare
      </button>
    </div>
  );
}
