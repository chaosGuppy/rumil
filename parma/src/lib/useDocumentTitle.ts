"use client";

import { useEffect } from "react";

// Set document.title for the current view. Inputs that are
// `null`/`undefined`/empty are dropped so callers can pass whatever they
// have without building the string themselves.
//
// Title shape: `<part1> · <part2> · ... | <suffix>`. Suffix defaults to
// "Rumil" — the project-wide brand for this reader UI. The operator pages
// pass their own suffix to differentiate.
//
// The effect is keyed on the computed string, so we only write when the
// title actually changes rather than on every rerender. We don't restore
// on unmount — whichever mounted page takes over next will overwrite.
const DEFAULT_SUFFIX = "Rumil";

export function useDocumentTitle(
  parts: Array<string | null | undefined>,
  suffix: string = DEFAULT_SUFFIX,
): void {
  const clean = parts
    .map((p) => (typeof p === "string" ? p.trim() : ""))
    .filter((p) => p.length > 0);
  const lead = clean.join(" · ");
  const title = lead ? `${lead} | ${suffix}` : suffix;

  useEffect(() => {
    if (typeof document === "undefined") return;
    document.title = title;
  }, [title]);
}
